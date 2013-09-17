#!/usr/bin/python2.7
#-*- coding=utf-8 -*-

# Copy Rights (c) Beijing TigerKnows Technology Co., Ltd.

"""描述实际执行任务的worker的行为和属性。

 Worker: 具体执行任务的worker类
 start_worker: 启动worker
 stop_worker: 关闭worker
 suspend_worker: 挂起worker
 rouse_worker: 唤醒worker
 get_all_workers: 获取所有的worker
 get_worker_statistic: 获取某一worker对应的实时统计信息
 recover_worker_from_broken: 以恢复模式启动worker
"""

__authors__ = ['"wuyadong" <wuyadong@tigerknows.com>']

import datetime
import uuid

from tornado import ioloop, gen

from core.util import logging, coroutine_wrap, get_class_path
from core.download import fetch
from core.datastruct import Task, Item
from core.statistic import WorkerStatistic, output_statistic_file, WORKER_STATISTIC_PATH
from core.record import record, RecorderManager

MAX_EMPTY_TASK_COUNT = 10  # worker最大能够获取的空Task个数


class WorkerError(Exception):
    """当worker内部发生异常时，将抛出workerError

    """
    pass


class Worker(object):
    """描述具体执行任务的worker的类

    Attributes:
        _workers: 字典，记录启动的所有worker
        _worker_index: 整型，描述下一个worker的编号
    """

    workers = {}

    def __init__(self, spider, worker_name):
        """使用spider和worker_name来初始化worker

            Args:
                spider: BaseSpider的一个实例
                worker_name: 字符串，worker的名字

        """

        self.logger = logging.getLogger(self.__class__.__name__)
        self.spider = spider
        self._worker_name = worker_name
        self.worker_statistic = WorkerStatistic()
        self.is_started = False
        self.is_suspended = False
        self._empty_task_count = 0

    def start(self):
        """启动这个worker
            启动的时候，会将spider中的start_tasks移到待抓取队列
            不会重复启动
        """
        if self.is_started:
            self.logger.warn("duplicate start")
        else:
            self.worker_statistic.start_time = datetime.datetime.now()
            RecorderManager.instance().record_doing(
                record(self._worker_name, self.worker_statistic.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                   get_class_path(self.spider.crawl_schedule.__class__),
                   self.spider.crawl_schedule.schedule_kwargs,
                   get_class_path(self.spider.__class__), self.spider.spider_kwargs))

            _move_start_tasks_to_crawl_schedule(self.spider.start_tasks,
                                            self.spider.crawl_schedule)
            self.is_started = True
            ioloop.IOLoop.instance().add_timeout(
                datetime.timedelta(milliseconds=self.spider.crawl_schedule.interval),
                self.loop_get_and_execute)
            self.logger.info("start worker")

    def recover(self):
        """以恢复模式启动这个worker
            不会重复启动
        """
        if self.is_started:
            self.logger.warn("duplicate start")
        else:
            self.worker_statistic.start_time = datetime.datetime.now()
            RecorderManager.instance().record_doing(
                record(self._worker_name, self.worker_statistic.start_time.strftime("%Y-%m-%d %H:%M:%S"),
                   get_class_path(self.spider.crawl_schedule.__class__),
                   self.spider.crawl_schedule.schedule_kwargs,
                   get_class_path(self.spider.__class__), self.spider.spider_kwargs))

            self.is_started = True
            ioloop.IOLoop.instance().add_timeout(
                datetime.timedelta(milliseconds=self.spider.crawl_schedule.interval),
                self.loop_get_and_execute)
            self.logger.info("start worker")

    def stop(self):
        """关闭这个worker，并保存统计信息
            关闭的时候，会清空所有schedule中的队列以及pipeline中的中间数据
            不会重复关闭
        """
        if not self.is_started:
            self.logger.warn("duplicate stop")
        else:
            self.is_started = False
            self.worker_statistic.end_time = datetime.datetime.now()
            RecorderManager.instance().record_done(self._worker_name)
            output_statistic_file(WORKER_STATISTIC_PATH, self.worker_statistic,
                                      self._worker_name, self.spider.__class__.__name__)
            self.spider.clear_all()
            self.logger.info("stop worker")

    def suspend(self):
        """暂停worker
        """

        if self.is_started:
            if self.is_suspended:
                self.logger.warn("duplicate suspend")
            else:
                self.is_suspended = True
                self.logger.info("suspend worker")
        else:
            self.logger.warn("stopped worker not permit to suspend")

    def rouse(self):
        """唤醒worker
        """

        if self.is_started:
            if self.is_suspended:
                self.is_suspended = False
                self.logger.info("rouse worker")
            else:
                self.logger.warn("worker is not suspended, so validate operate of "
                                 "rousing")
        else:
            self.logger.warn("stopped worker not permit to rouse")

    @gen.coroutine
    def fetch_and_extract(self, task):
        """抓取并解析
            采用的是异步技术
        """
        if not self.is_started:
            raise gen.Return

        self.worker_statistic.incre_processing_number()
        fetch_start_time = datetime.datetime.now()
        resp = yield fetch(task)
        fetch_time = datetime.datetime.now() - fetch_start_time
        self.worker_statistic.count_average_fetch_time(
            task.callback, fetch_start_time,fetch_time)

        if resp.code is 200 and resp.error is None:
            self.logger.debug("fetch success")
            self.worker_statistic.add_spider_success(task.callback + "-fetch")
            self.spider.crawl_schedule.flag_url_haven_done(task.request.url)
            self.extract(task, resp)
        else:
            self.logger.error("fetch request failed, code:%s error:%s url:%s" %
                              (resp.code, resp.error, task.request.url))
            task.reason = "fetch error, code:%s " % (resp.code, )
            self.worker_statistic.add_spider_retry(task.callback, task.reason)
            self.spider.crawl_schedule.handle_fail_task(task)

        self.worker_statistic.decre_processing_number()

    def extract(self, task, resp):
        """解析下载下来的数据
            同步技术
            Args:
                task: Task, 任务的描述
                resp: HTTPResponse, http结果
        """
        if not self.is_started:
            return
        extract_start_time = datetime.datetime.now()
        try:
            hrefs = self.spider.parse(task, resp)
        except Exception, e:
            self.logger.error("parser error:%s" % e)
            task.reason = "extract error"
            self.spider.crawl_schedule.handle_fail_task(task)
            self.worker_statistic.add_spider_retry(task.callback, task.reason)
        else:
            try:
                if hrefs is not None:
                    for item_or_task in hrefs:
                        if isinstance(item_or_task, Task):
                            self.spider.crawl_schedule.push_new_task(item_or_task)
                        if isinstance(item_or_task, Item):
                            try:
                                handle_start_time = datetime.datetime.now()
                                self.spider.handle_item(item_or_task, task.kwargs)
                            except Exception, e:
                                self.logger.error("handle error:%s" % e)
                                task.reason = "handle error"
                                self.spider.crawl_schedule.handle_fail_task(task)
                                self.worker_statistic.add_spider_retry(
                                    item_or_task.__class__.__name__, "Unsupported")
                            else:
                                self.worker_statistic.add_spider_success(
                                    "%s-%s" % (item_or_task.__class__.__name__, "handle"))
                                handle_interval = datetime.datetime.now() - handle_start_time
                                self.worker_statistic.count_average_handle_item_time(
                                    item_or_task.__class__.__name__, handle_start_time, handle_interval)

            except Exception, e:
                self.logger.error("extract url: %s error:%s" % (task.request.url, e))
                task.reason = "extract error"
                self.spider.crawl_schedule.handle_fail_task(task)
                self.worker_statistic.add_spider_retry(task.callback, task.reason)
            else:
                extract_time = datetime.datetime.now() - extract_start_time
                self.worker_statistic.count_average_extract_time(
                    task.callback, extract_start_time, extract_time)
                self.worker_statistic.add_spider_success(task.callback + "-extract")

    @gen.coroutine
    def loop_get_and_execute(self):
        """循环获取任务，并执行
            异步技术
        """
        if self.is_started:
            if not self.is_suspended and self.worker_statistic.processing_number \
                    < self.spider.crawl_schedule.max_number:
                task = yield coroutine_wrap(self.spider.crawl_schedule.pop_task)
                if isinstance(task, Exception):
                    self.logger.error("pop task error:%s" % task)
                else:
                    if task:
                        ioloop.IOLoop.instance().add_timeout(
                            datetime.timedelta(milliseconds=self.spider.crawl_schedule.interval),
                            self.loop_get_and_execute)
                        self._empty_task_count = 0
                        yield self.fetch_and_extract(task)
                    else:
                        if self.worker_statistic.processing_number <= 0:
                            self._empty_task_count += 1
                        if self._empty_task_count > MAX_EMPTY_TASK_COUNT:
                            self.stop()
                        self.logger.debug("empty request")
                        ioloop.IOLoop.instance().add_timeout(
                            datetime.timedelta(milliseconds=
                            self.spider.crawl_schedule.interval * 2),
                            self.loop_get_and_execute)
            else:
                ioloop.IOLoop.instance().add_timeout(
                    datetime.timedelta(milliseconds=self.spider.crawl_schedule.interval
                                                    * 3),
                    self.loop_get_and_execute)

def _move_start_tasks_to_crawl_schedule(start_tasks, crawl_schedule):
    """将种子任务转移到crawl_schedule中的待抓取队列

        Args:
            start_tasks: 任务集合
            crawl_schedule: CrawlSchedule的实例
    """
    for task in start_tasks:
        crawl_schedule.push_new_task(task)

def recover_worker(spider):
    """以恢复模式启动worker
        Args:
            spider: 描述抓取流程，BaseSpider的实例
        Raises:
            WorkerError: 创建worker失败
    """
    worker_name = "worker-%d" % uuid.uuid4()
    try:
        worker = Worker(spider, worker_name)
    except Exception, e:
        raise WorkerError("init worker error:%s" % e)

    try:
        worker.recover()
    except Exception, e:
        raise WorkerError("start worker error:%s" % e)
    else:
        Worker.workers[worker_name] = worker

def start_worker(spider):

    """启动一个worker
        Args:
            spider: 描述抓取流程，BaseSpider的实例.

        Raises:
            WorkerError: 创建worker失败
    """
    worker_name = "worker-%d" % uuid.uuid4()
    try:
        worker = Worker(spider, worker_name)
    except Exception, e:
        raise WorkerError("init worker error:%s" % e)

    try:
        worker.start()
    except Exception, e:
        raise WorkerError("start worker error:%s" % e)
    else:
        Worker.workers[worker_name] = worker

# def recover_worker_from_broken(spider):
#     """主要是将崩溃的worker尝试进行恢复
#         只是尝试进行恢复，不一定成功
#         实现原理：就是不对schedule进行清空，不将start_tasks转移
#
#     """
#
#     worker_name = "worker-%d" % Worker.worker_index
#     try:
#         worker = Worker(spider, worker_name)
#     except Exception, e:
#         raise WorkerError("init worker error:%s" % e)
#
#     try:
#         worker.recover()
#     except Exception, e:
#         raise WorkerError("recover worker error:%s" % e)
#     else:
#         Worker.workers[worker_name] = worker
#         Worker.worker_index += 1

def stop_worker(worker_name):

    """关闭一个worker

        Args:
            worker_name: 对应的worker的名字

        Raises:
            WorkerError: 当不存在这个worker，或者worker关闭出错
    """

    if not Worker.workers.has_key(worker_name):
        raise WorkerError("not has %s worker" % worker_name)
    worker = Worker.workers.get(worker_name)

    try:
        worker.stop()
    except Exception, e:
        raise WorkerError("stop worker error:%s" % e)


def suspend_worker(worker_name):

    """暂停一个worker
        Args:
            worker_name: worker的名字

        Raises:
            WorkerError: 当不存在这个worker，或者挂起失败
    """

    if not Worker.workers.has_key(worker_name):
        raise WorkerError("not has worker:%s" % worker_name)
    worker = Worker.workers.get(worker_name)
    try:
        worker.suspend()
    except Exception, e:
        raise WorkerError("suspend worker error:%s" % e)

def rouse_worker(worker_name):

    """唤醒worker
        Args:
            worker_name: worker的名字

        Raises:
            WorkerError: 当不存在worker或者唤醒失败

    """

    if not Worker.workers.has_key(worker_name):
        raise WorkerError("not has %s worker" % worker_name)
    worker = Worker.workers.get(worker_name)
    try:
        worker.rouse()
    except Exception, e:
        raise WorkerError("rouse worker error:%s" % e)

def get_all_workers():

    """获取所有worker的字典
        Returns:
            workers: 字典, key为worker_name，value是worker实例

    """
    workers = []
    for key, value in Worker.workers.iteritems():
        temp_worker = {}
        temp_worker['name'] = key
        if not value.is_started:
            status = "stopped"
        elif value.is_suspended:
            status = "suspended"
        else:
            status = "run"

        temp_worker['status'] = status
        temp_worker['spider_name'] = value.spider.__class__.__name__
        temp_worker['schedule_name'] = value.spider.crawl_schedule.__class__.__name__
        temp_worker['start_time'] = value.worker_statistic.start_time.strftime("%Y-%m-%d %H:%M:%S")
        workers.append(temp_worker)

    return workers

def get_worker_statistic(worker_name):

    """获取worker的实时统计数据
        Args:
            worker_name: worker的name

        Returns:
            worker_statistic: WorkerStatistic的实例

        Raises:
            WorkerError: 当worker不存在
    """
    if not Worker.workers.has_key(worker_name):
        raise WorkerError("not has %s worker" % worker_name)
    worker = Worker.workers.get(worker_name)
    return worker.worker_statistic