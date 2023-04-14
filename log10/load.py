import types
import functools
import inspect
import requests
import os
import json
import time
import traceback
from aiohttp import ClientSession
import asyncio
import threading
import queue
from contextlib import contextmanager
import logging
from dotenv import load_dotenv

load_dotenv()

url = os.environ.get("LOG10_URL")
token = os.environ.get("LOG10_TOKEN")
org_id = os.environ.get("LOG10_ORG_ID")

# log10, bigquery
target_service = os.environ.get("TARGET_SERVICE")

if target_service == "bigquery":
    from log10.bigquery import initialize_bigquery
    bigquery_client, bigquery_table = initialize_bigquery()
    import uuid
    from datetime import datetime, timezone


# Set this to True during debugging and False in production
DEBUG = False

logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO,
                    format='%(asctime)s - %(levelname)s - LOG10 - %(message)s')


def get_session_id():
    try:
        session_url = url + "/api/sessions"
        res = requests.request("POST",
                               session_url, headers={"x-log10-token": token, "Content-Type": "application/json"}, json={
                                   "organization_id": org_id
                               })

        return res.json()['sessionID']
    except Exception as e:
        raise Exception("Failed to create LOG10 session: " + str(e))


# Global variable to store the current sessionID.
sessionID = get_session_id()


class log10_session:
    def __enter__(self):
        global sessionID
        sessionID = get_session_id()

    def __exit__(self, exc_type, exc_value, traceback):
        return


@contextmanager
def timed_block(block_name):
    if DEBUG:
        start_time = time.perf_counter()
        try:
            yield
        finally:
            elapsed_time = time.perf_counter() - start_time
            logging.debug(
                f"TIMED BLOCK - {block_name} took {elapsed_time:.6f} seconds to execute.")
    else:
        yield


async def log_async(completion_url, func, **kwargs):
    async with ClientSession() as session:
        res = requests.request("POST",
                               completion_url, headers={"x-log10-token": token, "Content-Type": "application/json"}, json={
                                   "organization_id": org_id
                               })
        # todo: handle session id for bigquery scenario
        completionID = res.json()['completionID']
        log_row = {
            # do we want to also store args?
            "status": "started",
            "orig_module": func.__module__,
            "orig_qualname": func.__qualname__,
            "request": json.dumps(kwargs),
            "session_id": sessionID,
            "organization_id": org_id
        }
        if target_service == "log10":
            res = requests.request("POST",
                                   completion_url + "/" + completionID,
                                   headers={"x-log10-token": token,
                                            "Content-Type": "application/json"},
                                   json=log_row)
        elif target_service == "bigquery":
            log_row["id"] = str(uuid.uuid4())
            log_row["created_at"] = datetime.now(timezone.utc).isoformat()
            try:
                bigquery_client.insert_rows_json(bigquery_table, [log_row])
            except Exception as e:
                logging.error(
                    f"failed to insert in Bigquery: {log_row} with error {e}")

        return completionID


def run_async_in_thread(completion_url, func, result_queue, **kwargs):
    result = asyncio.run(
        log_async(completion_url=completion_url, func=func, **kwargs))
    result_queue.put(result)

# this function is deprecated but available for debugging; use the log_async function going forward


def log_sync(completion_url, func, **kwargs):
    res = requests.request("POST",
                           completion_url, headers={"x-log10-token": token, "Content-Type": "application/json"}, json={
                               "organization_id": org_id
                           })
    completionID = res.json()['completionID']

    res = requests.request("POST",
                           completion_url + "/" + completionID,
                           headers={"x-log10-token": token,
                                    "Content-Type": "application/json"},
                           json={
                               # do we want to also store args?
                               "status": "started",
                               "orig_module": func.__module__,
                               "orig_qualname": func.__qualname__,
                               "request": json.dumps(kwargs),
                               "session_id": sessionID,
                               "organization_id": org_id
                           })
    return completionID


def intercepting_decorator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        completion_url = url + "/api/completions"
        output = None
        result_queue = queue.Queue()

        try:
            with timed_block("async call duration"):
                threading.Thread(target=run_async_in_thread, kwargs={
                                 "completion_url": completion_url, "func": func, "result_queue": result_queue, **kwargs}).start()

            current_stack_frame = traceback.extract_stack()
            stacktrace = ([{"file": frame.filename,
                          "line": frame.line,
                           "lineno": frame.lineno,
                            "name": frame.name} for frame in current_stack_frame])

            start_time = time.perf_counter()
            output = func(*args, **kwargs)
            duration = time.perf_counter() - start_time
            logging.debug(f"TIMED BLOCK - OpenAI call duration: {duration}")

            with timed_block("extra time spent waiting for log10 call"):
                while result_queue.empty():
                    pass
                completionID = result_queue.get()

            with timed_block("result call duration (sync)"):
                log_row = {
                    "response": json.dumps(output),
                    "status": "finished",
                    "duration": int(duration*1000),
                    "stacktrace": json.dumps(stacktrace)
                }
                if target_service == "log10":
                    res = requests.request("POST",
                                           completion_url + "/" + completionID,
                                           headers={
                                               "x-log10-token": token, "Content-Type": "application/json"},
                                           json=log_row)
                elif target_service == "bigquery":
                    try:
                        # todo: need to change to append columns
                        bigquery_client.insert_rows_json(
                            bigquery_table, [log_row])
                    except Exception as e:
                        logging.error(
                            f"failed to insert in Bigquery: {log_row} with error {e}")
        except Exception as e:
            logging.error("failed", e)

        return output

    return wrapper


def log10(module):
    def intercept_nested_functions(obj):
        for name, attr in vars(obj).items():
            if callable(attr) and isinstance(attr, types.FunctionType):
                setattr(obj, name, intercepting_decorator(attr))
            elif inspect.isclass(attr):
                intercept_class_methods(attr)

    def intercept_class_methods(cls):
        for method_name, method in vars(cls).items():
            if isinstance(method, classmethod):
                original_method = method.__func__
                decorated_method = intercepting_decorator(original_method)
                setattr(cls, method_name, classmethod(decorated_method))
            elif isinstance(method, (types.FunctionType, types.MethodType)):
                setattr(cls, method_name, intercepting_decorator(method))
            elif inspect.isclass(method):  # Handle nested classes
                intercept_class_methods(method)

    for name, attr in vars(module).items():
        if callable(attr) and isinstance(attr, types.FunctionType):
            setattr(module, name, intercepting_decorator(attr))
        elif inspect.isclass(attr):  # Check if attribute is a class
            intercept_class_methods(attr)
        # else: # uncomment if we want to include nested function support
        #     intercept_nested_functions(attr)
