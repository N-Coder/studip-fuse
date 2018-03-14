import logging
import operator
import textwrap
from enum import IntEnum
from functools import reduce
from time import time
from types import SimpleNamespace
from typing import Any, List, NamedTuple, Union

import attr
from aiohttp import ClientSession, TraceConfig, TraceRequestEndParams, TraceRequestExceptionParams, TraceRequestStartParams
from attr import Factory
from cached_property import cached_property

__all__ = ["CircuitBreakerProperties", "Status", "HealthCount", "CircuitBreakerOpenException", "NetworkCircuitBreaker"]
log = logging.getLogger("studip_fuse.cache.circuit_breaker")


@attr.s()
class CircuitBreakerProperties(object):
    # TODO tune properties and make configurable
    # @formatter:off
    request_volume_threshold   = 10     # type: int   # 10 requests in 60 seconds must occur before statistics matter
    error_percentage_threshold = 50     # type: int   # 50 = if 50%+ of requests in 60 seconds are failures then we will trip the circuit
    summary_intervall          = 10     # type: int   # statisticalWindow: check error percentage every 10 seconds
    event_timeout              = 60     # type: int   # ignore events that are older than 60 seconds
    sleep_window               = 10     # type: int   # 5 seconds that we will sleep before trying again after tripping the circuit
    force_open                 = False  # type: bool  # false (we want to allow traffic)
    force_closed               = False  # type: bool  # ignoreErrors = false
    # @formatter:on


class Status(IntEnum):
    CLOSED = 1
    OPEN = 2
    HALF_OPEN = 3


Event = NamedTuple("Event", [('timestamp', float), ('success', bool), ('duration', float), ('content', Union[int, Exception])])


@attr.s()
class HealthCount(object):
    success_count = attr.ib()  # type: int
    error_count = attr.ib()  # type: int
    average_duration = attr.ib()  # type: float
    average_throughput = attr.ib()  # type: float

    @property
    def total_count(this):
        return this.success_count + this.error_count

    @property
    def error_percentage(this):
        if this.total_count > 0:
            return (this.error_count * 100.0) / this.total_count
        else:
            return 0

    def __add__(self, other):
        if isinstance(other, Event):
            return self + HealthCount(int(other.success), int(not other.success), other.duration,
                                      other.content if other.success else 0)
        elif isinstance(other, HealthCount):
            if self.average_duration and other.average_duration:
                duration = (self.average_duration + other.average_duration) / 2
            else:
                duration = self.average_duration or other.average_duration

            if self.average_throughput and other.average_throughput:
                throughput = (self.average_throughput + other.average_throughput) / 2
            else:
                throughput = self.average_throughput or other.average_throughput

            return HealthCount(self.success_count + other.success_count, self.error_count + other.error_count,
                               duration, throughput)
        else:
            raise ValueError("Can't add %s %s to %s" % (type(other), other, self))

    @staticmethod
    def empty():
        return HealthCount(0, 0, 0, 0)


class CircuitBreakerOpenException(Exception):
    pass


# noinspection PyMethodOverriding
@attr.s()
class NetworkCircuitBreaker(object):
    properties = attr.ib(default=CircuitBreakerProperties())  # type: CircuitBreakerProperties

    # TODO expose getter for status / setter for force open/closed
    status = attr.ib(init=False, default=Status.CLOSED)  # type: Status
    opened = attr.ib(init=False, default=-1)  # type: int

    events = attr.ib(init=False, default=Factory(list))  # type: List[Event]
    last_summary = attr.ib(init=False, default=-1)  # type: int

    @cached_property
    def trace_config(self):
        trace_config = TraceConfig()
        trace_config.on_request_start.append(self.on_request_start)
        trace_config.on_request_end.append(self.on_request_end)
        trace_config.on_request_exception.append(self.on_request_exception)
        return trace_config

    async def on_request_start(self, session: ClientSession, context: SimpleNamespace, params: TraceRequestStartParams):
        # TODO track request (lifecycle) times correctly, find generic way of improving / enforcing timeouts (and raise_for_status)
        # see https://github.com/aio-libs/aiohttp/issues/2768 and https://github.com/aio-libs/aiohttp/pull/2767
        # TODO kill request on circuit open?
        if not self.attempt_execution():
            log.debug("Blocking execution of request %s trough open circuit breaker", params)
            raise CircuitBreakerOpenException()
        else:
            log.debug("Allowing execution of request %s trough closed circuit breaker", params)
            context.start_time = time()

    async def on_request_end(self, session: ClientSession, context: SimpleNamespace, params: TraceRequestEndParams):
        # XXX a 400 or 500 HTTP response code will also be logged as "successful" response.
        # But all HTTP response should have raise_for_status() immediately called upon completion,
        # so those exceptions will probably be caught in the exception_handler.
        # As we don't care about Stud.IP load, only about network problems, keep firing on HTTP status 500.
        t = time()
        duration = t - context.start_time
        length = int(params.response.headers.get("content-length", 0))
        if not length:
            length = getattr(params.response.content, "total_bytes", 0)
        log.debug("Request was successful, took %ss for %sb (%s b/s):\n%s",
                  duration, length, length / duration, textwrap.indent(str(params), "\t"))
        self.on_event(t, True, duration, length)

        if self.status == Status.HALF_OPEN:
            log.info("Closed half-open circuit after successful request %s", str(params.response).replace("\n", ""))
            # this thread wins the race to close the circuit - it resets the stream to start it over from 0
            self.status = Status.CLOSED
            self.events.clear()
            self.last_summary = t
            self.opened = -1

    async def on_request_exception(self, session: ClientSession, context: SimpleNamespace, params: TraceRequestExceptionParams):
        if isinstance(params.exception, CircuitBreakerOpenException):
            return

        t = time()
        duration = t - context.start_time
        log.debug("Request failed, took %ss: %s", duration, params)
        self.on_event(t, False, duration, params.exception)

        if self.status == Status.HALF_OPEN:
            log.info("Opened half-open circuit again after failed request %s", params.exception)
            # this thread wins the race to re-open the circuit - it resets the start time for the sleep window
            self.status = Status.OPEN
            self.opened = t

    def exception_handler(self, exc_type, exception, traceback) -> Any:
        raise exception

    def may_create(self, key, args, kwargs) -> bool:
        return self.allow_request()

    def on_event(self, *args, **kwargs):
        event = Event(*args, **kwargs)
        timestamp = event.timestamp
        self.events.append(event)

        if self.last_summary + self.properties.summary_intervall < timestamp:
            self.last_summary = timestamp
            old_len_events = len(self.events)
            self.events = [e for e in self.events if e.timestamp + self.properties.event_timeout > timestamp]
            hc = reduce(operator.add, self.events, HealthCount.empty())
            log.debug("Dropped %s expired events and got new %s", old_len_events - len(self.events), hc)
            if hc.total_count >= self.properties.request_volume_threshold \
                    and hc.error_percentage >= self.properties.error_percentage_threshold:
                # our failure rate is too high, we need to set the state to OPEN
                if self.status == Status.CLOSED:
                    log.info("Opened closed circuit after too many failed request (%s%% of %s)",
                             hc.error_percentage, hc.total_count)
                    self.status = Status.OPEN
                    self.opened = timestamp
            else:
                #    we are not past the minimum volume threshold for the stat window
                # or we are not past the minimum error threshold for the stat window,
                # so no change to circuit status.
                # if it was CLOSED, it stays CLOSED
                # if it was half-open, we need to wait for a successful command execution
                # if it was open, we need to wait for sleep window to elapse
                pass

    @property
    def is_open(self):
        if self.properties.force_open:
            return True
        if self.properties.force_closed:
            return False
        return self.opened >= 0

    def can_probe_half_open(self):
        # TODO also quit sleep window if NetworkManager Connectivity changes or user requests reconnect
        return time() > self.opened + self.properties.sleep_window

    def allow_request(self):
        if self.properties.force_open:
            return False
        if self.properties.force_closed:
            return True
        if self.opened == -1:
            return True
        else:
            if self.status == Status.HALF_OPEN:
                return False
            else:
                return self.can_probe_half_open()

    def attempt_execution(self):
        if self.properties.force_open:
            return False
        if self.properties.force_closed:
            return True
        if self.opened == -1:
            return True
        else:
            if self.can_probe_half_open():
                # only the first request after sleep window should execute
                # if the executing command succeeds, the status will transition to CLOSED
                # if the executing command fails, the status will transition to OPEN
                if self.status == Status.OPEN:
                    log.info("Half-closed open circuit for probe request")
                    self.status = Status.HALF_OPEN
                    return True
                else:
                    return False
            else:
                return False
