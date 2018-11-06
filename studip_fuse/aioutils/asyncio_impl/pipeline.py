import asyncio
from asyncio import Queue

from async_generator import async_generator, yield_

from studip_fuse.aioutils.interface import Pipeline


# TODO instead of using queues, the async funcs could also be replaced by chained async_generators
# TODO this could be replaced by aioreactive/ reactive python


class AsyncioPipeline(Pipeline):
    done_obj = object()

    def __init__(self):
        self.queues = [Queue()]
        self.tasks = []

    def put(self, item):
        self.queues[0].put_nowait(item)

    @async_generator
    async def drain(self):
        self.queues[0].put_nowait(self.done_obj)
        await asyncio.gather(*self.tasks)

        queue = self.queues[-1]
        while True:
            item = await queue.get()
            try:
                if item is self.done_obj:
                    break
                else:
                    await yield_(item)
            finally:
                queue.task_done()

    async def __processor(self, in_queue, out_queue, func):
        while True:
            item = await in_queue.get()
            try:
                if item is self.done_obj:
                    out_queue.put_nowait(self.done_obj)
                    break
                else:
                    await func(item, out_queue)
            finally:
                in_queue.task_done()

    def add_processor(self, func):
        in_queue = self.queues[-1]
        out_queue = Queue()
        self.queues.append(out_queue)
        self.tasks.append(self.__processor(in_queue=in_queue, out_queue=out_queue, func=func))
