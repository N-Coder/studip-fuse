import asyncio
import logging
import sys
import threading
import traceback

from more_itertools import one

log = logging.getLogger("studip_fuse.event_loop")


def await_loop_thread_shutdown(loop, loop_thread):
    # print loop stack trace until the loop thread completed
    counter = 0
    while loop_thread.is_alive() and counter < 4:
        loop_thread.join(5)
        counter += 1
        if loop_thread.is_alive():
            if loop:
                dump_loop_stack(loop)
            else:
                dump_thread_stack(loop_thread)

    if loop_thread.is_alive():
        log.warning("Shutting down main thread and thus killing hung event loop daemon thread")


def format_stack(stack):
    return "".join(traceback.format_stack(stack[0] if isinstance(stack, list) else stack))


def dump_loop_stack(loop):
    current_task = asyncio.Task.current_task(loop=loop)
    pending_tasks = [t for t in asyncio.Task.all_tasks(loop=loop) if not t.done() and t is not current_task]
    loop_thread = one(t for t in threading.enumerate() if t.ident == loop._thread_id)
    loop_thread_stack = sys._current_frames()[loop_thread.ident]
    loop_thread_stack_trace = format_stack(loop_thread_stack)

    if log.isEnabledFor(logging.DEBUG):
        log.debug("Current task %s in loop %s in loop thread %s", current_task, loop, loop_thread)
        if current_task:
            log.debug("Task stack trace:\n %s", format_stack(current_task.get_stack()))
        log.debug("Thread stack trace:\n %s", loop_thread_stack_trace)
        pending_tasks_str = "\n".join(
            str(t) + "\n" + format_stack(t.get_stack())
            for t in pending_tasks)
        log.debug("%s further pending tasks:\n %s", len(pending_tasks), pending_tasks_str)
    else:
        log.info("Waiting for event loop to stop... (%s further pending tasks after current task %s "
                 "in loop %s in loop thread %s)", len(pending_tasks), current_task, loop, loop_thread)

    if len(pending_tasks) == 0 and current_task is None \
            and "epoll.poll(timeout, max_ev)" in loop_thread_stack_trace.strip().split("\n")[-1]:
        log.warning("Event loop hangs in epoll selector without any tasks pending. "
                    "This is probably a python bug (https://bugs.python.org/issue29780).")


def dump_thread_stack(loop_thread):
    log.info("Waiting for loop thread to abort initialization...")
    if log.isEnabledFor(logging.DEBUG):
        log.debug("Thread stack trace:\n %s", format_stack(sys._current_frames()[loop_thread.ident]))
