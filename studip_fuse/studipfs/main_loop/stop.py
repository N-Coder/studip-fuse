import asyncio
import logging
import signal
import sys
import threading
import traceback
from collections import defaultdict

from more_itertools import one

log = logging.getLogger(__name__)


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
    scheduled = getattr(loop, "_scheduled", None)

    if log.isEnabledFor(logging.DEBUG):
        log.debug("Current task %s in loop %s in loop thread %s", current_task, loop, loop_thread)
        if current_task:
            log.debug("Task stack trace:\n %s", format_stack(current_task.get_stack()))
        log.debug("Thread stack trace:\n %s", loop_thread_stack_trace)
        pending_tasks_str = "\n".join(
            str(t) + "\n" + format_stack(t.get_stack())
            for t in pending_tasks)
        log.debug("%s further pending tasks:\n %s", len(pending_tasks), pending_tasks_str)
        if scheduled is not None:
            log.debug("%s further scheduled tasks:\n %s", len(scheduled), scheduled)
    else:
        log.info("Waiting for event loop to stop... (%s further pending tasks after current task %s "
                 "in loop %s in loop thread %s)", len(pending_tasks), current_task, loop, loop_thread)

    if len(pending_tasks) == 0 and not scheduled and current_task is None \
            and "epoll.poll(timeout, max_ev)" in loop_thread_stack_trace.strip().split("\n")[-1]:
        log.warning("Event loop hangs in epoll selector without any tasks pending. "
                    "Sending SIGINT to event loop thread...")
        try:
            signal.pthread_kill(loop_thread.ident, signal.SIGINT)
            siginfo = signal.sigtimedwait([signal.SIGINT], 0)
            if siginfo:
                log.debug("Ignoring signal %s delivered to this thread instead of event loop thread...", siginfo)
        except InterruptedError:
            log.debug("Ignoring InterruptedError delivered to this thread instead of event loop thread...")
            pass


def dump_thread_stack(loop_thread):
    log.info("Waiting for loop thread to abort initialization...")
    if log.isEnabledFor(logging.DEBUG):
        log.debug("Thread stack trace:\n %s", format_stack(sys._current_frames()[loop_thread.ident]))


# TODO move
class ThreadSafeDefaultDict(defaultdict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__lock = threading.Lock()

    def __missing__(self, key):
        with self.__lock:
            if key in self:
                return super().__getitem__(key)
            else:
                return super().__missing__(key)
