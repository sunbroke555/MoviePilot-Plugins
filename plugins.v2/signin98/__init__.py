import os
import random
import re
import traceback
from pathlib import Path

import time
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Page, Error
from cf_clearance import sync_stealth
from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
import http.cookies


class SignIn98(_PluginBase):
    # 插件名称
    plugin_name = "98签到"
    # 插件描述
    plugin_desc = "98签到。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/98tang.png"
    # 插件版本
    plugin_version = "1.1.2"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "signin98_"
    # 加载顺序
    plugin_order = 24
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _random_delay = None
    _cookie = None
    _onlyonce = False
    _notify = False
    _history_days = None
    _host = None
    _fid = None
    _ua = None
    _replies = None
    _comment = None
    _proxy = None
    # 签到成功文件
    SIGN_SUCCESS_FILE: str = None
    # 评论成功文件
    COMMENT_SUCCESS_FILE: str = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._random_delay = config.get("random_delay")
            self._cookie = config.get("cookie")
            self._notify = config.get("notify")
            self._host = config.get("host")
            self._fid = config.get("fid")
            self._replies = config.get("replies")
            self._ua = config.get("ua")
            self._onlyonce = config.get("onlyonce")
            self._comment = config.get("comment")
            self._proxy = config.get("proxy")
            self._history_days = config.get("history_days") or 30

        # 签到成功文件
        self.SIGN_SUCCESS_FILE: str = os.path.join(self.get_data_path(), "sign_success.json")
        # 评论成功文件
        self.COMMENT_SUCCESS_FILE: str = os.path.join(self.get_data_path(), "comment_success.json")

        # 定时服务
        self._scheduler = BackgroundScheduler(timezone=settings.TZ)

        if self._onlyonce:
            logger.info(f"98签到服务启动，立即运行一次")
            self._scheduler.add_job(func=self.__signin, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                    name="98签到")
            # 关闭一次性开关
            self._onlyonce = False
            self.update_config({
                "onlyonce": False,
                "cron": self._cron,
                "enabled": self._enabled,
                "cookie": self._cookie,
                "notify": self._notify,
                "host": self._host,
                "replies": self._replies,
                "fid": self._fid,
                "ua": self._ua,
                "proxy": self._proxy,
                "history_days": self._history_days,
                "random_delay": self._random_delay,
                "comment": self._comment
            })
        else:
            try:
                self._scheduler.add_job(func=self.__signin,
                                        trigger=CronTrigger.from_crontab(str(self._cron)),
                                        name="98签到" + f"随机延时{self._random_delay}秒" if self._random_delay else "",
                                        args=[self._random_delay])
            except Exception as err:
                logger.error(f"定时任务配置错误：{err}")
                # 推送实时消息
                self.systemmessage.put(f"执行周期配置错误：{err}")

        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def __signin(self, random_delay=None):
        """
        98签到
        """
        if random_delay:
            random_delay = random.randint(int(str(random_delay).split("-")[0]), int(str(random_delay).split("-")[1]))
            logger.info(f"随机延时 {random_delay} 秒")
            time.sleep(random_delay)

        with sync_playwright() as playwright:
            browser = playwright["chromium"].launch(headless=False)
            context = browser.new_context(user_agent=self._ua, proxy={"server": self._proxy})
            cookie_dict = http.cookies.SimpleCookie(self._cookie)
            cookie_dict = [{'name': key, 'value': morsel.value, 'url': f"https://{self._host}"} for key, morsel in
                           cookie_dict.items()]
            context.add_cookies(cookie_dict)
            page = context.new_page()

            try:
                # 刷积分任务
                if self._comment:
                    if self._comment.count("-") == 1:
                        start_cnt, end_cnt = self._comment.split("-")
                        comment_cnt = random.randint(int(start_cnt), int(end_cnt))
                    elif self._comment.isdigit():
                        comment_cnt = int(self._comment)
                    else:
                        comment_cnt = 0

                    if comment_cnt:
                        logger.info(f"开始进行{comment_cnt}次评论任务，随机延迟，请耐心等待。")
                        for i in range(comment_cnt):
                            # 发布每日评论
                            self.__do_comment(page)
                            wait_time = random.randint(20, 30)
                            logger.info(f"随机等待 {wait_time} 秒")
                            time.sleep(wait_time)

                # 签到任务
                msg = self.start_sign(page)

                # 发送通知
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【98签到任务完成】",
                        text=msg)

                # 读取历史记录
                history = self.get_data('history') or []

                history.append({
                    "date": datetime.today().strftime('%Y-%m-%d %H:%M:%S'),
                    "msg": msg
                })

                thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
                history = [record for record in history if
                           datetime.strptime(record["date"],
                                             '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
                # 保存签到历史
                self.save_data(key="history", value=history)
            except Exception as e:
                logger.error(f"错误原因：{str(e)}")
            finally:
                browser.close()

    def daysign(self, page) -> str:
        """
        开始签到
        :return:
        """
        now = datetime.now()
        logger.info(f"{now.strftime('%Y-%m-%d')} 开始98堂签到")

        # 判断当天是否评论成功
        comment_flag = False
        if Path(self.COMMENT_SUCCESS_FILE).exists():
            # 尝试加载本地
            with open(self.COMMENT_SUCCESS_FILE, 'r') as file:
                content = file.read()
                if content and str(content) == now.strftime('%Y-%m-%d'):
                    logger.info("今日已评论，开始签到逻辑")
                    comment_flag = True

        success_flag = False
        retry = 0
        sign_result = None
        while not success_flag and retry < 3:
            try:
                if not comment_flag:
                    # 评论一次
                    self.__do_comment(page)

                    wait_time = random.randint(3, 10)
                    logger.info(f"随机等待 {wait_time} 秒")
                    time.sleep(wait_time)

                # 签到一次
                success_flag, sign_result = self.__do_sign(page)
                if not success_flag:
                    # 重试签到3次
                    for i in range(3):
                        if retry == 3:
                            break
                        logger.info(f"第 {i + 1} 次重试签到")
                        success_flag, sign_result = self.__do_sign(page)
                        if not success_flag:
                            retry += 1
                        else:
                            if '签到成功' in sign_result:
                                break
            except Exception as e:
                logger.error(f"获取网页源码失败: {str(e)}, 稍后重试第 {retry + 2} 次")
                wait_time = random.randint(2, 5)
                logger.info(f"随机等待 {wait_time} 秒")
                time.sleep(wait_time)
                sign_result = None

        return sign_result

    def __do_comment(self, page):
        """
        发布每日评论
        :return:
        """
        fid = random.choice(self._fid.split(","))
        logger.info(f"随机访问专区 {fid}")

        # 请求专区获取所有帖子id
        fid_html_res = self.get_page_source(
            url=f'https://{self._host}/forum.php?mod=forumdisplay&fid={fid}',
            page=page)

        if not fid_html_res:
            return f"访问 {fid} 专区失败"
        tids = re.findall(r"normalthread_(\d+)", fid_html_res,
                          re.MULTILINE | re.IGNORECASE)
        tid = random.choice(tids)
        logger.info(f"随机访问 {fid} 专区帖子id {tid}")

        wait_time = random.randint(3, 10)
        logger.info(f"随机等待 {wait_time} 秒")
        time.sleep(wait_time)

        # 请求帖子获取formhash id
        tid_html_res = self.get_page_source(
            url=f'https://{self._host}/forum.php?mod=viewthread&tid={tid}&extra=page%3D1',
            page=page)
        if not tid_html_res:
            return f"访问 {fid} 专区 {tid} 帖子失败"

        soup = BeautifulSoup(tid_html_res, 'html.parser')
        formhash = soup.find('input', {'name': 'formhash'})['value']
        logger.info(f"获取到 {fid} 专区 {tid} 帖子formhash {formhash}")

        message = random.choice(self._replies.split("\n"))
        logger.info(f'获取到随机评论 {message}')

        wait_time = random.randint(10, 20)
        logger.info(f"随机等待 {wait_time} 秒")
        time.sleep(wait_time)

        # 找到 <textarea> 元素
        textarea = page.locator('#fastpostmessage')
        # 向 <textarea> 元素添加内容
        textarea.fill(message)

        # 点击发布按钮
        button = page.locator('//*[@id="fastpostsubmit"]')
        button.click()

        page.wait_for_timeout(1500)

        # 回复发布成功
        if '回复发布成功' in page.content():
            logger.info(f"发送 {fid} 专区 {tid} 帖子评论 {message} 成功")
            self.__sava_json(self.COMMENT_SUCCESS_FILE)

    def __do_sign(self, page):
        """
        执行签到逻辑
        :param headers:
        :return:
        """
        # 0、获取签到页面formhash
        sign_html_res = self.get_page_source(url=f'https://{self._host}/plugin.php?id=dd_sign', page=page)
        if not sign_html_res:
            return False, f"访问签到页面失败"

        wait_time = random.randint(1, 4)
        logger.info(f"随机等待 {wait_time} 秒")
        time.sleep(wait_time)

        # 点击签到按钮
        button = page.locator('.ddpc_sign_btn_red')
        if not button:
            return False, "获取签到按钮失败"
        button.click()

        page.wait_for_timeout(1500)

        # 获取问题
        question = page.evaluate('''() => {
            return document.querySelector('.rfm td').textContent.trim();
        }''')
        question = question.replace('换一个', '').replace('?', '').replace('=', '').strip()
        logger.info(f"获取到签到问题 {question}")

        ans = eval(question)
        assert type(ans) == int
        logger.info(f"解析到验证码 {question} 答案 {ans}")

        wait_time = random.randint(1, 3)
        logger.info(f"随机等待 {wait_time} 秒")
        time.sleep(wait_time)

        # 填写答案
        input = page.locator('input[name="secanswer"]')
        # 向 <textarea> 元素添加内容
        input.fill(str(ans))

        # 点击签到按钮
        button = page.locator('button[name="signsubmit"]')
        if not button:
            return False, "提交签到失败"
        button.click()

        page.wait_for_timeout(1500)

        if '签到成功' in page.content():
            return True, "签到成功，获得2金钱。"
        else:
            return False, "校验签到验证码失败"

    def __sava_json(self, file):
        """
        保存签到成功｜评论成功json文件
        """
        logger.info(f"开始写入本地文件 {file}")
        file = open(file, 'w')
        file.write(datetime.now().strftime('%Y-%m-%d'))
        file.close()

    def __get_user_profile(self, page):
        """
        获取用户信息
        :return:
        """
        home_html_res = self.get_page_source(
            url=f'https://{self._host}/home.php?mod=space', page=page)
        if not home_html_res:
            return ""

        # 使用BeautifulSoup解析HTML
        soup = BeautifulSoup(home_html_res, 'html.parser')

        # 获取用户组信息
        user_group = soup.select_one('li:-soup-contains("用户组")').text.strip()

        # 获取积分信息
        points = soup.select_one('li:-soup-contains("积分")').text.strip()

        # 获取金钱信息
        money = soup.select_one('li:-soup-contains("金钱")').text.strip()

        return f"{user_group} {points} {money}"

    @staticmethod
    def __pass_cloudflare(url: str, page: Page) -> bool:
        """
        尝试跳过cloudfare验证
        """
        sync_stealth(page, pure=True)
        page.goto(url)
        success = False
        cf = True
        tries = 10
        user_tries = tries
        while tries > 0:
            page.wait_for_timeout(1500)
            try:
                success = False if page.query_selector("#challenge-form") or page.query_selector(
                    "#challenge-running") else True
                if success:
                    break
                for target_frame in page.main_frame.child_frames:
                    if "challenge" in target_frame.url and "turnstile" in target_frame.url:
                        try:
                            click = target_frame.query_selector(
                                "xpath=//input[@type='checkbox']"
                            )
                        except Error:
                            # frame is refreshed, so playwright._impl._api_types.Error: Target closed
                            logger.debug("Playwright Error:", exc_info=True)
                        else:
                            if click:
                                click.click()
                                page.wait_for_timeout(1500)
            except Error:
                logger.debug("Playwright Error:", exc_info=True)
                success = False
            tries -= 1
        if tries == user_tries:
            cf = False
        return success, cf

    def get_page_source(self,
                        url: str,
                        timeout: int = 60,
                        page=None) -> str:
        """
        获取网页源码
        :param url: 网页地址
        """
        source = ""
        try:
            try:
                if not self.__pass_cloudflare(url, page):
                    logger.warn("cloudflare challenge fail！")
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)
                # 找到元素
                element = page.query_selector('.enter-btn')

                # 已满18周岁提示
                if element:
                    element.click()
                    page.wait_for_timeout(1500)
                    source = page.content()
                else:
                    source = page.content()
            except Exception as e:
                logger.error(f"获取网页源码失败: {str(e)}")
                source = None
        except Exception as e:
            logger.error(f"获取网页源码失败: {str(e)}")
        return source

    def start_sign(self, page):
        """
        开始签到
        :return:
        """
        try:
            # 判断当天是否签到成功
            now = datetime.now()
            if Path(self.SIGN_SUCCESS_FILE).exists():
                # 尝试加载本地
                with open(self.SIGN_SUCCESS_FILE, 'r') as file:
                    content = file.read()
                    if content and str(content) == now.strftime('%Y-%m-%d'):
                        msg = self.__get_user_profile(page)
                        logger.info(f"今日已签到。{msg}")
                        print(f"今日已签到。{msg}")
                        return

            raw_html = self.daysign(page)

            if not raw_html:
                return f"获取网站源码失败"

            if '签到成功' in raw_html:
                message_text = raw_html
                self.__sava_json(self.SIGN_SUCCESS_FILE)
                message_text += self.__get_user_profile(page)
            else:
                message_text = raw_html
        except IndexError:
            message_text = f'正则匹配错误'
        except Exception as e:
            message_text = f'错误原因：{e}'
            # log detailed error message
            traceback.print_exc()

        logger.info(message_text)
        return message_text

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "InvitesSignin",
                "name": "药丸签到服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__signin,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'random_delay',
                                            'label': '随机延迟',
                                            'placeholder': '1-3'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '保留历史天数'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'host',
                                            'label': '98 Host'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'fid',
                                            'label': '专区FID'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'ua',
                                            'label': 'user-agent'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'comment',
                                            'label': '评论次数',
                                            'placeholder': '1-3或者1'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'proxy',
                                            'label': '自定义代理'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '98 Cookie'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'replies',
                                            'label': '自动回复',
                                            'rows': 5,
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "cookie": "",
            "proxy": settings.PROXY_HOST,
            "host": "sehuatang.org",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "history_days": 7,
            "random_delay": '60-3600',
            "fid": '2,36,103',
            "cron": "0 9 * * *",
            "comment": "1-3",
            "replies": "感谢分享\n感谢分享!\n感谢分享。\n感谢楼主\n感谢感谢\n感谢感谢！\n感谢感谢。\n谢谢分享\n谢谢楼主\n感谢楼主分享\n爱了爱了\n感谢分享\n楼主万岁！\n爱了爱了！！！\n赞！！！\n感谢\n非常不错\n支持支持\n感谢分享\n感谢楼主分享好片\n感谢分享！！\n感谢分享感谢分享\n必须支持\n感谢分享啊\n封面还不错\n谢谢！支持一波\n看着不错\n支持一波\n真不错啊\n不错不错\n感謝分享\n分享支持。\n感谢大佬分享\n看着不错\n感谢老板分享\n谢谢分享！！！"
        }

    def get_page(self) -> List[dict]:
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        # 按照签到时间倒序
        historys = sorted(historys, key=lambda x: x.get("date") or 0, reverse=True)

        # 签到消息
        sign_msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("date")
                    },
                    {
                        'component': 'td',
                        'text': history.get("msg")
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '签到信息'
                                            }
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': sign_msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))
