import asyncio
import logging


class OneShotTasks:
    def __init__(self, logger: logging.Logger) -> None:
        self._tasks = []
        self._logger = logger

    def add(self, coro, description: str) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda task: self._remove_task(task, description))
        return task

    async def stop(self):
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def _remove_task(self, task: asyncio.Task, description: str) -> None:
        try:
            self._tasks.remove(task)
        except ValueError:
            # Task might have been removed already (e.g., during stop()).
            pass
        if not task.cancelled() and task.exception() is not None:
            self._logger.error("%s raised an exception: %s", description, task.exception())
