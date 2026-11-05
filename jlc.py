import os
import sys
import time
import json
import tempfile
import random
import requests
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from serverchan_sdk import sc_send

# ===================== 全局控制：跳过随机等待 =====================
SKIP_RANDOM_SLEEP = os.getenv("SKIP_RANDOM_SLEEP", "").lower() == "true"

def smart_sleep(sec):
    """如果开启SKIP_RANDOM_SLEEP则不等待，否则sleep"""
    if not SKIP_RANDOM_SLEEP:
        time.sleep(sec)

def smart_sleep_random(min_t, max_t):
    if SKIP_RANDOM_SLEEP:
        return
    t = random.uniform(min_t, max_t)
    time.sleep(t)
# =================================================================

in_summary = False
summary_logs = []

def log(msg):
    full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)

def format_nickname(nickname):
    if not nickname or len(nickname.strip()) == 0:
        return "未知用户"
    nickname = nickname.strip()
    if len(nickname) == 1:
        return f"{nickname}*"
    elif len(nickname) == 2:
        return f"{nickname[0]}*"
    else:
        return f"{nickname[0]}{'*' * (len(nickname)-2)}{nickname[-1]}"

def with_retry(func, max_retries=5, delay=1):
    def wrapper(*args, **kwargs):
        for attempt in range(max_retries):
            try:
                result = func(*args, **kwargs)
                if result is not None:
                    return result
                smart_sleep(delay + random.uniform(0, 1))
            except Exception:
                smart_sleep(delay + random.uniform(0, 1))
        return None
    return wrapper

@with_retry
def extract_token_from_local_storage(driver):
    try:
        token = driver.execute_script("return window.localStorage.getItem('X-JLC-AccessToken');")
        if token:
            log(f"✅ 成功从 localStorage 提取 token: {token[:30]}...")
            return token
        alternative_keys = [
            "x-jlc-accesstoken",
            "accessToken",
            "token",
            "jlc-token"
        ]
        for key in alternative_keys:
            token = driver.execute_script(f"return window.localStorage.getItem('{key}');")
            if token:
                log(f"✅ 从 localStorage 的 {key} 提取到 token: {token[:30]}...")
                return token
    except Exception as e:
        log(f"⚠️ 从 localStorage 提取 token 失败: {e}")
    return None

# ===================== 重构secretkey获取：保留selenium3 performance日志，多次解析 =====================
@with_retry
def extract_secretkey_from_devtools(driver):
    secretkey = None
    all_perf_logs = []
    try:
        raw_logs = driver.get_log('performance')
        all_perf_logs.extend(raw_logs)
    except Exception as e:
        log(f"⚠️ 获取performance日志异常 {e}")

    for entry in all_perf_logs:
        try:
            message = json.loads(entry['message'])
            msg = message.get('message', {})
            method = msg.get('method', '')
            params = msg.get('params', {})

            if method == 'Network.requestWillBeSent':
                req = params.get('request', {})
                url = req.get('url', '')
                if 'm.jlc.com' not in url:
                    continue
                headers = req.get('headers', {})
                for k in ["secretkey","SecretKey","secretKey","SECRETKEY"]:
                    sk = headers.get(k)
                    if sk:
                        secretkey = sk
                        log(f"✅ 请求头提取secretkey: {secretkey[:20]}...")
                        return secretkey

            elif method == 'Network.responseReceived':
                resp = params.get('response', {})
                url = resp.get('url','')
                if 'm.jlc.com' not in url:
                    continue
                req_headers = resp.get('requestHeaders',{})
                for k in ["secretkey","SecretKey","secretKey","SECRETKEY"]:
                    sk = req_headers.get(k)
                    if sk:
                        secretkey = sk
                        log(f"✅ response requestHeaders提取secretkey: {secretkey[:20]}...")
                        return secretkey
        except Exception:
            continue
    log("⚠️ devtools网络日志未捕获secretkey")
    return secretkey

def try_refetch_secretkey(driver):
    """辅助函数：刷新页面交互后重新抓取secretkey"""
    sk = extract_secretkey_from_devtools(driver)
    if sk:
        return sk
    driver.refresh()
    smart_sleep_random(1.5,3)
    return extract_secretkey_from_devtools(driver)
# =================================================================

def get_oshwhub_points(driver, account_index):
    max_retries = 5
    for attempt in range(max_retries):
        try:
            cookies = driver.get_cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            headers = {
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'accept': 'application/json, text/plain, */*',
                'cookie': cookie_str
            }
            response = requests.get("https://oshwhub.com/api/users", headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data and data.get('success'):
                    points = data.get('result', {}).get('points', 0)
                    return points
        except Exception:
            pass
        if attempt < max_retries -1:
            try:
                driver.refresh()
                WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                smart_sleep_random(1,2)
            except:
                pass
    log(f"账号 {account_index} - ⚠️ 无法获取积分信息")
    return 0

class JLCClient:
    def __init__(self, access_token, secretkey, account_index, driver):
        self.base_url = "https://m.jlc.com"
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'x-jlc-clienttype': 'WEB',
            'accept': 'application/json, text/plain, */*',
            'x-jlc-accesstoken': access_token,
            'secretkey': secretkey,
            'Referer': 'https://m.jlc.com/mapp/pages/my/index',
        }
        self.account_index = account_index
        self.driver = driver
        self.message = ""
        self.initial_jindou = 0
        self.final_jindou = 0
        self.jindou_reward = 0
        self.sign_status = "未知"
        self.has_reward = False

    def send_request(self, url, method='GET'):
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=self.headers, timeout=10)
            else:
                response = requests.post(url, headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                log(f"账号 {self.account_index} - ⚠️ 请求失败，状态码: {response.status_code}")
                return None
        except Exception as e:
            log(f"账号 {self.account_index} - ⚠️ 请求异常 ({url}): {e}")
            return None

    def get_user_info(self):
        log(f"账号 {self.account_index} - 获取用户信息...")
        url = f"{self.base_url}/api/appPlatform/center/setting/selectPersonalInfo"
        data = self.send_request(url)
        if data and data.get('success'):
            log(f"账号 {self.account_index} - ✅ 用户信息获取成功")
            return True
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ⚠️ 获取用户信息失败: {error_msg}")
            return False

    def get_points(self):
        url = f"{self.base_url}/api/activity/front/getCustomerIntegral"
        max_retries = 5
        for attempt in range(max_retries):
            data = self.send_request(url)
            if data and data.get('success'):
                jindou_count = data.get('data', {}).get('integralVoucher', 0)
                return jindou_count
            if attempt < max_retries -1:
                try:
                    self.driver.get("https://m.jlc.com/")
                    self.driver.refresh()
                    WebDriverWait(self.driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                    smart_sleep_random(1,2)
                    navigate_and_interact_m_jlc(self.driver, self.account_index)
                    access_token = extract_token_from_local_storage(self.driver)
                    secretkey = try_refetch_secretkey(self.driver)
                    if access_token:
                        self.headers['x-jlc-accesstoken'] = access_token
                    if secretkey:
                        self.headers['secretkey'] = secretkey
                except Exception:
                    pass
        log(f"账号 {self.account_index} - ⚠️ 获取金豆数量失败")
        return 0

    def check_sign_status(self):
        log(f"账号 {self.account_index} - 检查签到状态...")
        url = f"{self.base_url}/api/activity/sign/getCurrentUserSignInConfig"
        data = self.send_request(url)
        if data and data.get('success'):
            have_sign_in = data.get('data', {}).get('haveSignIn', False)
            if have_sign_in:
                log(f"账号 {self.account_index} - ✅ 今日已签到")
                self.sign_status = "已签到过"
                return True
            else:
                log(f"账号 {self.account_index} - 今日未签到")
                self.sign_status = "未签到"
                return False
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ⚠️ 检查签到状态失败: {error_msg}")
            self.sign_status = "检查失败"
            return None

    def sign_in(self):
        log(f"账号 {self.account_index} - 执行签到...")
        url = f"{self.base_url}/api/activity/sign/signIn?source=4"
        data = self.send_request(url)
        if data and data.get('success'):
            gain_num = data.get('data', {}).get('gainNum')
            if gain_num:
                log(f"账号 {self.account_index} - ✅ 签到成功，签到使金豆+{gain_num}")
                self.sign_status = "签到成功"
                return True
            else:
                log(f"账号 {self.account_index} - 有奖励可领取，先领取奖励")
                self.has_reward = True
                if self.receive_voucher():
                    log(f"账号 {self.account_index} - ✅ 奖励领取成功，签到完成")
                    self.sign_status = "领取奖励成功"
                    return True
                else:
                    self.sign_status = "领取奖励失败"
                    return False
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ⚠️ 签到失败: {error_msg}")
            self.sign_status = "签到失败"
            return False

    def receive_voucher(self):
        log(f"账号 {self.account_index} - 领取奖励...")
        url = f"{self.base_url}/api/activity/sign/receiveVoucher"
        data = self.send_request(url)
        if data and data.get('success'):
            log(f"账号 {self.account_index} - ✅ 领取成功")
            return True
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ⚠️ 领取奖励失败: {error_msg}")
            return False

    def calculate_jindou_difference(self):
        self.jindou_reward = self.final_jindou - self.initial_jindou
        if self.jindou_reward > 0:
            reward_text = f" (+{self.jindou_reward})"
            if self.has_reward:
                reward_text += "（有奖励）"
            log(f"账号 {self.account_index} - 📈 总金豆增加: {self.initial_jindou} → {self.final_jindou}{reward_text}")
        elif self.jindou_reward == 0:
            log(f"账号 {self.account_index} - ℹ️ 总金豆无变化，可能今天已签到过: {self.initial_jindou} → {self.final_jindou} (0)")
        else:
            log(f"账号 {self.account_index} - ⚠️ 金豆减少: {self.initial_jindou} → {self.final_jindou} ({self.jindou_reward})")
        return self.jindou_reward

    def execute_full_process(self):
        if not self.get_user_info():
            return False
        smart_sleep_random(1,2)
        self.initial_jindou = self.get_points()
        if self.initial_jindou is None:
            self.initial_jindou = 0
        log(f"账号 {self.account_index} - 签到前金豆💰: {self.initial_jindou}")
        smart_sleep_random(1,2)
        sign_status = self.check_sign_status()
        if sign_status is None:
            return False
        elif sign_status:
            log(f"账号 {self.account_index} - 今日已签到，跳过签到操作")
        else:
            smart_sleep_random(2,3)
            if not self.sign_in():
                return False
        smart_sleep_random(1,2)
        self.final_jindou = self.get_points()
        if self.final_jindou is None:
            self.final_jindou = 0
        log(f"账号 {self.account_index} - 签到后金豆💰: {self.final_jindou}")
        self.calculate_jindou_difference()
        return True

def navigate_and_interact_m_jlc(driver, account_index):
    log(f"账号 {account_index} - 在 m.jlc.com 进行交互操作...")
    try:
        WebDriverWait(driver,12).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        driver.execute_script("window.scrollTo(0, 300);")
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        nav_selectors = [
            "//div[contains(text(), '我的')]",
            "//div[contains(text(), '个人中心')]",
            "//div[contains(text(), '用户中心')]",
            "//a[contains(@href, 'user')]",
            "//a[contains(@href, 'center')]",
        ]
        for selector in nav_selectors:
            try:
                element = WebDriverWait(driver,5).until(EC.element_to_be_clickable((By.XPATH, selector)))
                element.click()
                log(f"账号 {account_index} - 点击导航元素: {selector}")
                WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                break
            except Exception:
                continue
        driver.execute_script("window.scrollTo(0, 500);")
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        driver.refresh()
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
    except Exception as e:
        log(f"账号 {account_index} - 交互操作出错: {e}")

def is_sunday():
    return datetime.now().weekday() == 6

def is_last_day_of_month():
    today = datetime.now()
    next_month = today.replace(day=28) + timedelta(days=4)
    last_day = next_month - timedelta(days=next_month.day)
    return today.day == last_day.day

def capture_reward_info(driver, account_index, gift_type):
    try:
        reward_elem = WebDriverWait(driver,5).until(
            EC.presence_of_element_located((By.XPATH, '//p[contains(text(), "恭喜获取")]'))
        )
        reward_text = reward_elem.text.strip()
        gift_name = "七日礼包" if gift_type == "7天" else "月度礼包"
        log(f"账号 {account_index} - {gift_name}领取结果：{reward_text}")
        return f"开源平台{gift_name}领取结果: {reward_text}"
    except Exception as e:
        log(f"账号 {account_index} - 已点击{gift_type}好礼，未获取到奖励信息(可能已领取过或未达到领取条件)，请自行前往开源平台查看。")
        return None

def click_gift_buttons(driver, account_index):
    reward_results = []
    if not is_sunday() and not is_last_day_of_month():
        return reward_results
    try:
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        log(f"账号 {account_index} - 开始点击礼包按钮...")
        sunday = is_sunday()
        last_day = is_last_day_of_month()
        if sunday:
            try:
                seven_day_gift = driver.find_element(By.XPATH, '//div[contains(@class, "sign_text__r9zaN")]/span[text()="7天好礼"]')
                seven_day_gift.click()
                log(f"账号 {account_index} - ✅ 检测到今天是周日，成功点击7天好礼，祝你周末愉快~")
                WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                reward_result = capture_reward_info(driver, account_index, "7天")
                if reward_result:
                    reward_results.append(reward_result)
                if last_day:
                    driver.refresh()
                    WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                    smart_sleep(12)
            except Exception as e:
                log(f"账号 {account_index} - ⚠️ 无法点击7天好礼: {e}")
        if last_day:
            try:
                monthly_gift = driver.find_element(By.XPATH, '//div[contains(@class, "sign_text__r9zaN")]/span[text()="月度好礼"]')
                monthly_gift.click()
                log(f"账号 {account_index} - ✅ 检测到今天是月底，成功点击月度好礼～")
                WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                reward_result = capture_reward_info(driver, account_index, "月度")
                if reward_result:
                    reward_results.append(reward_result)
            except Exception as e:
                log(f"账号 {account_index} - ⚠️ 无法点击月度好礼: {e}")
    except Exception as e:
        log(f"账号 {account_index} - ⚠️ 点击礼包按钮时出错: {e}")
    return reward_results

@with_retry
def get_user_nickname_from_api(driver, account_index):
    try:
        cookies = driver.get_cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'accept': 'application/json, text/plain, */*',
            'cookie': cookie_str
        }
        response = requests.get("https://oshwhub.com/api/users", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data and data.get('success'):
                nickname = data.get('result', {}).get('nickname', '')
                if nickname:
                    formatted_nickname = format_nickname(nickname)
                    log(f"账号 {account_index} - 👤 昵称: {formatted_nickname}")
                    return formatted_nickname
        log(f"账号 {account_index} - ⚠️ 无法获取用户昵称")
        return None
    except Exception as e:
        log(f"账号 {account_index} - ⚠️ 获取用户昵称失败: {e}")
        return None

# ===================== 核心修复：ensure_login_page 直接访问passport登录页 =====================
def ensure_login_page(driver, account_index):
    max_restarts = 5
    restarts = 0
    while restarts < max_restarts:
        try:
            driver.get("https://passport.jlc.com/login")
            log(f"账号 {account_index} - 直接打开 passport.jlc.com/login 登录页")
            WebDriverWait(driver,15).until(lambda d: "passport.jlc.com/login" in d.current_url)
            current_url = driver.current_url
            if "passport.jlc.com/login" in current_url:
                log(f"账号 {account_index} - ✅ 检测到登录页面")
                return True
            else:
                restarts +=1
                if restarts >= max_restarts:
                    log(f"账号 {account_index} - ⚠️ 重启浏览器{max_restarts}次后仍无法进入登录页面")
                    return False
                driver.quit()
                chrome_options = Options()
                chrome_options.add_argument("--headless=new")
                chrome_options.add_argument("--no-sandbox")
                chrome_options.add_argument("--disable-dev-shm-usage")
                chrome_options.add_argument("--disable-gpu")
                chrome_options.add_argument("--window-size=1920,1080")
                chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
                chrome_options.add_argument("--disable-blink-features=AutomationControlled")
                chrome_options.add_argument("--blink-settings=imagesEnabled=false")
                chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
                chrome_options.add_experimental_option('useAutomationExtension', False)
                caps = DesiredCapabilities.CHROME
                caps['goog:loggingPrefs'] = {'performance': 'ALL'}
                driver = webdriver.Chrome(options=chrome_options, desired_capabilities=caps)
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                smart_sleep(2)
        except Exception as e:
            restarts += 1
            if restarts >= max_restarts:
                log(f"账号 {account_index} - ⚠️ 重启浏览器{max_restarts}次后仍出现异常: {e}")
                return False
            try:
                driver.quit()
            except Exception:
                pass
            chrome_options = Options()
            chrome_options.add_argument("--headless=new")
            chrome_options.add_argument("--no-sandbox")
            chrome_options.add_argument("--disable-dev-shm-usage")
            chrome_options.add_argument("--disable-gpu")
            chrome_options.add_argument("--window-size=1920,1080")
            chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
            chrome_options.add_argument("--disable-blink-features=AutomationControlled")
            chrome_options.add_argument("--blink-settings=imagesEnabled=false")
            chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
            chrome_options.add_experimental_option('useAutomationExtension', False)
            caps = DesiredCapabilities.CHROME
            caps['goog:loggingPrefs'] = {'performance': 'ALL'}
            driver = webdriver.Chrome(options=chrome_options, desired_capabilities=caps)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            smart_sleep(2)
    return False

def check_password_error(driver, account_index):
    try:
        error_selectors = [
            "//*[contains(text(), '账号或密码不正确')]",
            "//*[contains(text(), '用户名或密码错误')]",
            "//*[contains(text(), '密码错误')]",
            "//*[contains(text(), '登录失败')]",
            "//*[contains(@class, 'error')]",
            "//*[contains(@class, 'err-msg')]",
            "//*[contains(@class, 'toast')]",
            "//*[contains(@class, 'message')]"
        ]
        for selector in error_selectors:
            try:
                error_element = WebDriverWait(driver,2).until(
                    EC.presence_of_element_located((By.XPATH, selector))
                )
                if error_element.is_displayed():
                    error_text = error_element.text.strip()
                    if any(k in error_text for k in ['账号或密码不正确','用户名或密码错误','密码错误','登录失败']):
                        log(f"账号 {account_index} - ⚠️ 检测到账号或密码错误，跳过此账号")
                        return True
            except Exception:
                continue
        return False
    except Exception as e:
        log(f"账号 {account_index} - ⚠️ 检查密码错误时出现异常: {e}")
        return False

# ===================== 更新滑块代码：真人变速+抖动+回弹 =====================
def drag_slider_humanlike(driver, slider_element, track_element):
    """增强版滑块模拟：分段变速，横向纵向微小抖动，末端回弹"""
    track_width = track_element.size['width']
    slider_width = slider_element.size['width']
    move_distance = track_width - slider_width - 8
    actions = ActionChains(driver)
    actions.click_and_hold(slider_element).perform()
    smart_sleep_random(0.3,0.6)

    segments = [
        (move_distance * 0.65, 0.15, 0.35),
        (move_distance * 0.25, 0.1, 0.25),
        (move_distance * 0.10, 0.25, 0.45),
    ]
    moved = 0
    for dist_ratio, min_delay, max_delay in segments:
        dx = int(dist_ratio)
        dy = random.randint(-3,3)
        actions.move_by_offset(dx, dy).perform()
        moved += dx
        smart_sleep_random(min_delay, max_delay)

    back_offset = random.randint(-4,-1)
    actions.move_by_offset(back_offset, random.randint(-2,2)).perform()
    smart_sleep_random(0.08,0.18)
    actions.release().perform()
    log(f"滑块拖动完成，总距离 {move_distance}px，末端回弹 {back_offset}px")
    return True

def sign_in_account(username, password, account_index, total_accounts, retry_count=0, is_final_retry=False):
    retry_label = ""
    if retry_count > 0:
        retry_label = f" (重试{retry_count})"
    if is_final_retry:
        retry_label = " (最终重试)"
    log(f"开始处理账号 {account_index}/{total_accounts}{retry_label}")

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    caps = DesiredCapabilities.CHROME
    caps['goog:loggingPrefs'] = {'performance': 'ALL'}

    driver = webdriver.Chrome(options=chrome_options, desired_capabilities=caps)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    wait = WebDriverWait(driver,25)

    result = {
        'account_index': account_index,
        'nickname': '未知',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'initial_points': 0,
        'final_points': 0,
        'points_reward': 0,
        'reward_results': [],
        'jindou_status': '未知',
        'jindou_success': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': retry_count,
        'is_final_retry': is_final_retry,
        'password_error': False
    }
    try:
        if not ensure_login_page(driver, account_index):
            result['oshwhub_status'] = '无法进入登录页'
            return result
        current_url = driver.current_url
        log(f"账号 {account_index} - 检测到未登录状态，正在执行登录流程...")
        try:
            phone_btn = wait.until(
                EC.element_to_be_clickable((By.XPATH, '//button[contains(text(),"账号登录")]'))
            )
            phone_btn.click()
            log(f"账号 {account_index} - 已切换账号登录")
            WebDriverWait(driver,10).until(EC.presence_of_element_located((By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]')))
        except Exception as e:
            log(f"账号 {account_index} - 账号登录按钮可能已默认选中: {e}")
        try:
            user_input = wait.until(
                EC.presence_of_element_located((By.XPATH, '//input[@placeholder="请输入手机号码 / 客户编号 / 邮箱"]'))
            )
            user_input.clear()
            user_input.send_keys(username)
            pwd_input = wait.until(
                EC.presence_of_element_located((By.XPATH, '//input[@type="password"]'))
            )
            pwd_input.clear()
            pwd_input.send_keys(password)
            log(f"账号 {account_index} - 已输入账号密码")
        except Exception as e:
            log(f"账号 {account_index} - ⚠️ 登录输入框未找到: {e}")
            result['oshwhub_status'] = '登录失败'
            return result
        try:
            login_btn = wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button.submit"))
            )
            login_btn.click()
            log(f"账号 {account_index} - 已点击登录按钮")
        except Exception as e:
            log(f"账号 {account_index} - ⚠️ 登录按钮定位失败: {e}")
            result['oshwhub_status'] = '登录失败'
            return result

        smart_sleep(1)
        if check_password_error(driver, account_index):
            result['password_error'] = True
            result['oshwhub_status'] = '密码错误'
            return result

        # ============【修复滑块：探测是否存在，不存在直接跳过】============
        slider_exist = False
        try:
            slider = WebDriverWait(driver,3).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".btn_slide"))
            )
            track = WebDriverWait(driver,3).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".nc_scale"))
            )
            slider_exist = True
        except Exception:
            log(f"账号 {account_index} - 本次登录未触发滑块验证码，跳过滑块逻辑")

        if slider_exist:
            log(f"账号 {account_index} - 检测到滑块验证码，开始真人模拟滑动")
            drag_slider_humanlike(driver, slider, track)
            smart_sleep(1)
            if check_password_error(driver, account_index):
                result['password_error'] = True
                result['oshwhub_status'] = '密码错误'
                return result

        # 统一等待登录跳转
        try:
            WebDriverWait(driver,15).until(lambda d: "oshwhub.com" in d.current_url and "passport.jlc.com" not in d.current_url)
        except Exception as e:
            log(f"账号 {account_index} - 登录跳转等待异常: {e}")
        # =================================================================

        log(f"账号 {account_index} - 等待登录跳转...")
        max_wait = 15
        jumped = False
        for i in range(max_wait):
            current_url = driver.current_url
            if "oshwhub.com" in current_url and "passport.jlc.com" not in current_url:
                log(f"账号 {account_index} - 成功跳转回签到页面")
                jumped = True
                break
            smart_sleep(1)
        if not jumped:
            current_title = driver.title
            log(f"账号 {account_index} - ⚠️ 跳转超时，当前页面标题: {current_title}")
            result['oshwhub_status'] = '跳转失败'
            return result

        smart_sleep(1)
        nickname = get_user_nickname_from_api(driver, account_index)
        if nickname:
            result['nickname'] = nickname
        else:
            result['nickname'] = '未知'

        initial_points = get_oshwhub_points(driver, account_index)
        result['initial_points'] = initial_points if initial_points is not None else 0
        log(f"账号 {account_index} - 签到前积分⭐: {result['initial_points']}")

        log(f"账号 {account_index} - 正在签到中...")
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        try:
            driver.refresh()
            WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        except Exception:
            pass
        smart_sleep(6)

        try:
            try:
                signed_element = driver.find_element(By.XPATH, '//span[contains(text(),"已签到")]')
                log(f"账号 {account_index} - ✅ 今天已经在开源平台签到过了！")
                result['oshwhub_status'] = '已签到过'
                result['oshwhub_success'] = True
                result['reward_results'] = click_gift_buttons(driver, account_index)
            except Exception:
                signed = False
                max_attempts =5
                for attempt in range(max_attempts):
                    try:
                        sign_btn = wait.until(
                            EC.element_to_be_clickable((By.XPATH, '//span[contains(text(),"立即签到")]'))
                        )
                        sign_btn.click()
                        smart_sleep(2)
                        driver.refresh()
                        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                        smart_sleep(2)
                        driver.find_element(By.XPATH, '//span[contains(text(),"已签到")]')
                        signed = True
                        break
                    except Exception:
                        continue
                if signed:
                    log(f"账号 {account_index} - ✅ 开源平台签到成功！")
                    result['oshwhub_status'] = '签到成功'
                    result['oshwhub_success'] = True
                    WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
                    result['reward_results'] = click_gift_buttons(driver, account_index)
                else:
                    log(f"账号 {account_index} - ⚠️ 开源平台签到失败")
                    result['oshwhub_status'] = '签到失败'
        except Exception as e:
            log(f"账号 {account_index} - ⚠️ 开源平台签到异常: {e}")
            result['oshwhub_status'] = '签到异常'

        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        final_points = get_oshwhub_points(driver, account_index)
        result['final_points'] = final_points if final_points is not None else 0
        log(f"账号 {account_index} - 签到后积分⭐: {result['final_points']}")
        result['points_reward'] = result['final_points'] - result['initial_points']
        if result['points_reward'] >0:
            log(f"账号 {account_index} - 📈 总积分增加: {result['initial_points']} → {result['final_points']} (+{result['points_reward']})")
        elif result['points_reward'] ==0:
            log(f"账号 {account_index} - ℹ️ 总积分无变化，可能今天已签到过: {result['initial_points']} → {result['final_points']} (0)")
        else:
            log(f"账号 {account_index} - ⚠️ 积分减少: {result['initial_points']} → {result['final_points']} ({result['points_reward']})")

        log(f"账号 {account_index} - 开始金豆签到流程...")
        driver.get("https://m.jlc.com/")
        log(f"账号 {account_index} - 已访问 m.jlc.com，等待页面加载...")
        WebDriverWait(driver,10).until(EC.presence_of_element_located((By.TAG_NAME,"body")))
        navigate_and_interact_m_jlc(driver, account_index)

        access_token = extract_token_from_local_storage(driver)
        secretkey = try_refetch_secretkey(driver)

        result['token_extracted'] = bool(access_token)
        result['secretkey_extracted'] = bool(secretkey)

        if access_token and secretkey:
            log(f"账号 {account_index} - ✅ 成功提取 token 和 secretkey")
            jlc_client = JLCClient(access_token, secretkey, account_index, driver)
            jindou_success = jlc_client.execute_full_process()
            result['jindou_success'] = jindou_success
            result['jindou_status'] = jlc_client.sign_status
            result['initial_jindou'] = jlc_client.initial_jindou
            result['final_jindou'] = jlc_client.final_jindou
            result['jindou_reward'] = jlc_client.jindou_reward
            result['has_jindou_reward'] = jlc_client.has_reward
            if jindou_success:
                log(f"账号 {account_index} - ✅ 金豆签到流程完成")
            else:
                log(f"账号 {account_index} - ⚠️ 金豆签到流程失败")
        else:
            log(f"账号 {account_index} - ⚠️ 无法提取到 token 或 secretkey，跳过金豆签到")
            result['jindou_status'] = 'Token提取失败'
    except Exception as e:
        log(f"账号 {account_index} - ⚠️ 程序执行错误: {e}")
        result['oshwhub_status'] = '执行异常'
    finally:
        driver.quit()
        log(f"账号 {account_index} - 浏览器已关闭")
    return result

def should_retry(merged_success, password_error):
    need_retry = (not merged_success['oshwhub'] or not merged_success['jindou']) and not password_error
    return need_retry

def process_single_account(username, password, account_index, total_accounts):
    max_retries = 3
    merged_result = {
        'account_index': account_index,
        'nickname': '未知',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'initial_points': 0,
        'final_points': 0,
        'points_reward': 0,
        'reward_results': [],
        'jindou_status': '未知',
        'jindou_success': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': 0,
        'is_final_retry': False,
        'password_error': False
    }
    merged_success = {'oshwhub': False, 'jindou': False}
    for attempt in range(max_retries +1):
        result = sign_in_account(username, password, account_index, total_accounts, retry_count=attempt)
        if result.get('password_error'):
            merged_result['password_error'] = True
            merged_result['oshwhub_status'] = '密码错误'
            merged_result['nickname'] = '未知'
            break
        if result['oshwhub_success'] and not merged_success['oshwhub']:
            merged_success['oshwhub'] = True
            merged_result['oshwhub_status'] = result['oshwhub_status']
            merged_result['initial_points'] = result['initial_points']
            merged_result['final_points'] = result['final_points']
            merged_result['points_reward'] = result['points_reward']
            merged_result['reward_results'] = result['reward_results']
        if result['jindou_success'] and not merged_success['jindou']:
            merged_success['jindou'] = True
            merged_result['jindou_status'] = result['jindou_status']
            merged_result['initial_jindou'] = result['initial_jindou']
            merged_result['final_jindou'] = result['final_jindou']
            merged_result['jindou_reward'] = result['jindou_reward']
            merged_result['has_jindou_reward'] = result['has_jindou_reward']
        if merged_result['nickname'] == '未知' and result['nickname'] != '未知':
            merged_result['nickname'] = result['nickname']
        if not merged_result['token_extracted'] and result['token_extracted']:
            merged_result['token_extracted'] = result['token_extracted']
        if not merged_result['secretkey_extracted'] and result['secretkey_extracted']:
            merged_result['secretkey_extracted'] = result['secretkey_extracted']
        merged_result['retry_count'] = result['retry_count']
        if not should_retry(merged_success, merged_result['password_error']) or attempt >= max_retries:
            break
        else:
            wait_t = random.randint(2,6)
            log(f"账号 {account_index} - 🔄 准备第 {attempt +1} 次重试，等待 {wait_t} 秒后重新开始...")
            smart_sleep(wait_t)
    merged_result['oshwhub_success'] = merged_success['oshwhub']
    merged_result['jindou_success'] = merged_success['jindou']
    return merged_result

def execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts):
    log("="*70)
    log("🔄 执行最终重试 - 处理所有重试后仍失败的账号")
    log("="*70)
    failed_accounts = []
    for i, result in enumerate(all_results):
        if (not result['oshwhub_success'] or not result['jindou_success']) and not result.get('password_error', False):
            failed_accounts.append({
                'index':i,
                'account_index': result['account_index'],
                'username': usernames[result['account_index']-1],
                'password': passwords[result['account_index']-1],
                'previous_retry_count': result['retry_count']
            })
    if not failed_accounts:
        log("✅ 没有需要最终重试的账号")
        return all_results
    log(f"🔍 需要最终重试的账号: {', '.join(str(acc['account_index']) for acc in failed_accounts)}")
    wait_time = random.randint(2,3)
    log(f"ℹ️ 等待 {wait_time} 秒后开始最终重试...")
    smart_sleep(wait_time)
    for failed_acc in failed_accounts:
        log(f"🔄 开始最终重试账号 {failed_acc['account_index']}")
        final_result = sign_in_account(
            failed_acc['username'],
            failed_acc['password'],
            failed_acc['account_index'],
            total_accounts,
            retry_count=failed_acc['previous_retry_count']+1,
            is_final_retry=True
        )
        if final_result.get('password_error'):
            original_result = all_results[failed_acc['index']]
            original_result['password_error'] = True
            original_result['oshwhub_status'] = '密码错误'
            original_result['nickname'] = '未知'
            original_result['is_final_retry'] = True
            original_result['retry_count'] = failed_acc['previous_retry_count']+1
            log(f"账号 {failed_acc['account_index']} - ⚠️ 最终重试检测到密码错误")
            continue
        original_result = all_results[failed_acc['index']]
        if final_result['oshwhub_success'] and not original_result['oshwhub_success']:
            original_result['oshwhub_success'] = True
            original_result['oshwhub_status'] = final_result['oshwhub_status']
            original_result['initial_points'] = final_result['initial_points']
            original_result['final_points'] = final_result['final_points']
            original_result['points_reward'] = final_result['points_reward']
            original_result['reward_results'] = final_result['reward_results']
            log(f"✅ 账号 {failed_acc['account_index']} - 开源平台签到成功")
        if final_result['jindou_success'] and not original_result['jindou_success']:
            original_result['jindou_success'] = True
            original_result['jindou_status'] = final_result['jindou_status']
            original_result['initial_jindou'] = final_result['initial_jindou']
            original_result['final_jindou'] = final_result['final_jindou']
            original_result['jindou_reward'] = final_result['jindou_reward']
            original_result['has_jindou_reward'] = final_result['has_jindou_reward']
            log(f"✅ 账号 {failed_acc['account_index']} - 金豆签到成功")
        if original_result['nickname'] == '未知' and final_result['nickname'] != '未知':
            original_result['nickname'] = final_result['nickname']
        if not original_result['token_extracted'] and final_result['token_extracted']:
            original_result['token_extracted'] = final_result['token_extracted']
        if not original_result['secretkey_extracted'] and final_result['secretkey_extracted']:
            original_result['secretkey_extracted'] = final_result['secretkey_extracted']
        original_result['is_final_retry'] = True
        original_result['retry_count'] = failed_acc['previous_retry_count']+1
        if failed_acc != failed_accounts[-1]:
            wt = random.randint(3,5)
            log(f"ℹ️ 等待 {wt} 秒后处理下一个重试账号...")
            smart_sleep(wt)
    log("✅ 最终重试完成")
    return all_results

def push_summary():
    if not summary_logs:
        return
    title = "嘉立创签到总结"
    text = "\n".join(summary_logs)
    full_text = f"{title}\n{text}"
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if telegram_bot_token and telegram_chat_id:
        try:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            params = {'chat_id': telegram_chat_id, 'text': full_text}
            requests.get(url, params=params)
            log("Telegram-日志已推送")
        except Exception:
            pass
    wechat_webhook_key = os.getenv('WECHAT_WEBHOOK_KEY')
    if wechat_webhook_key:
        try:
            if wechat_webhook_key.startswith("https://"):
                url = wechat_webhook_key
            else:
                url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wechat_webhook_key}"
            body = {"msgtype":"text","text":{"content":full_text}}
            requests.post(url, json=body)
            log("企业微信-日志已推送")
        except Exception:
            pass
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')
    if dingtalk_webhook:
        try:
            if dingtalk_webhook.startswith("https://"):
                url = dingtalk_webhook
            else:
                url = f"https://oapi.dingtalk.com/robot/send?access_token={dingtalk_webhook}"
            body = {"msgtype":"text","text":{"content":full_text}}
            requests.post(url, json=body)
            log("钉钉-日志已推送")
        except Exception:
            pass
    pushplus_token = os.getenv('PUSHPLUS_TOKEN')
    if pushplus_token:
        try:
            url = "http://www.pushplus.plus/send"
            body = {"token":pushplus_token,"title":title,"content":text}
            requests.post(url, json=body)
            log("PushPlus-日志已推送")
        except Exception:
            pass
    serverchan_sckey = os.getenv('SERVERCHAN_SCKEY')
    if serverchan_sckey:
        try:
            url = f"https://sctapi.ftqq.com/{serverchan_sckey}.send"
            body = {"title":title,"desp":text}
            requests.post(url, data=body)
            log("Server酱-日志已推送")
        except Exception:
            pass
    serverchan3_sckey = os.getenv('SERVERCHAN3_SCKEY')
    if serverchan3_sckey:
        try:
            textSC3 = "\n\n".join(summary_logs)
            resp = sc_send(serverchan3_sckey, title, textSC3, {"tags":"嘉立创|签到"})
            if resp.get("code") ==0:
                log("Server酱3-日志已推送")
            else:
                log(f"Server酱推送失败: {resp.get('message')}")
        except Exception as e:
            log(f"Server酱推送异常: {str(e)}")
    coolpush_skey = os.getenv('COOLPUSH_SKEY')
    if coolpush_skey:
        try:
            url = f"https://push.xuthus.cc/send/{coolpush_skey}?c={full_text}"
            requests.get(url)
            log("酷推-日志已推送")
        except Exception:
            pass
    custom_webhook = os.getenv('CUSTOM_WEBHOOK')
    if custom_webhook:
        try:
            body = {"title":title,"content":text}
            requests.post(custom_webhook, json=body)
            log("自定义API-日志已推送")
        except Exception:
            pass

def main():
    global in_summary
    # 优先读取环境变量 JLC_USERNAME JLC_PASSWORD（单账号）
    env_username = os.getenv("JLC_USERNAME","")
    env_password = os.getenv("JLC_PASSWORD","")

    usernames = []
    passwords = []
    enable_failure_exit = False

    # 如果命令行参数传入，优先使用命令行；没有则读取环境变量
    if len(sys.argv) >=3:
        usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
        passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
        if len(sys.argv) >= 4:
            enable_failure_exit = (sys.argv[3].lower() == 'true')
    else:
        # 没有传入命令行，读取环境变量单账号
        if env_username and env_password:
            usernames = [env_username]
            passwords = [env_password]
        else:
            print("用法: python jlc.py 账号1,账号2,账号3... 密码1,密码2,密码3... [失败退出标志]")
            print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3")
            print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3 true")
            print("也可设置环境变量 JLC_USERNAME JLC_PASSWORD 使用单账号模式")
            sys.exit(1)

    log(f"失败退出功能: {'开启' if enable_failure_exit else '关闭'}")
    log(f"跳过随机等待: {'开启' if SKIP_RANDOM_SLEEP else '关闭'}")

    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)

    total_accounts = len(usernames)
    log(f"开始处理 {total_accounts} 个账号的签到任务")
    all_results = []
    for i,(username,password) in enumerate(zip(usernames,passwords),1):
        log(f"开始处理第 {i} 个账号")
        result = process_single_account(username, password, i, total_accounts)
        all_results.append(result)
        if i < total_accounts:
            wt = random.randint(3,5)
            log(f"等待 {wt} 秒后处理下一个账号...")
            smart_sleep(wt)
    has_failed_accounts = any((not r['oshwhub_success'] or not r['jindou_success']) and not r.get('password_error',False) for r in all_results)
    if has_failed_accounts:
        all_results = execute_final_retry_for_failed_accounts(all_results, usernames, passwords, total_accounts)
    log("="*70)
    in_summary = True
    log("📋 详细签到任务完成总结")
    log("="*70)
    oshwhub_success_count = 0
    jindou_success_count =0
    total_points_reward =0
    total_jindou_reward =0
    retried_accounts = []
    password_error_accounts = []
    failed_accounts = []
    for result in all_results:
        account_index = result['account_index']
        nickname = result.get('nickname','未知')
        retry_count = result.get('retry_count',0)
        is_final_retry = result.get('is_final_retry',False)
        password_error = result.get('password_error',False)
        if password_error:
            password_error_accounts.append(account_index)
        if retry_count>0 or is_final_retry:
            retried_accounts.append(account_index)
        if (not result['oshwhub_success'] or not result['jindou_success']) and not password_error:
            failed_accounts.append(account_index)
        retry_label = ""
        if retry_count>0:
            retry_label = f" [重试{retry_count}次]"
        elif is_final_retry:
            retry_label = " [最终重试]"
        if password_error:
            log(f"账号 {account_index} (未知) 详细结果: [密码错误]")
            log("  └── 状态: ⚠️ 账号或密码错误，跳过此账号")
        else:
            log(f"账号 {account_index} ({nickname}) 详细结果:{retry_label}")
            log(f"  ├── 开源平台: {result['oshwhub_status']}")
            if result['points_reward']>0:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (+{result['points_reward']})")
                total_points_reward += result['points_reward']
            elif result['points_reward'] ==0 and result['initial_points']>0:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (0)")
            else:
                log(f"  ├── 积分状态: 无法获取积分信息")
            log(f"  ├── 金豆签到: {result['jindou_status']}")
            if result['jindou_reward']>0:
                jindou_text = f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (+{result['jindou_reward']})"
                if result['has_jindou_reward']:
                    jindou_text += "（有奖励）"
                log(jindou_text)
                total_jindou_reward += result['jindou_reward']
            elif result['jindou_reward'] ==0 and result['initial_jindou']>0:
                log(f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (0)")
            else:
                log(f"  ├── 金豆状态: 无法获取金豆信息")
            for rr in result['reward_results']:
                log(f"  ├── {rr}")
            if result['oshwhub_success']:
                oshwhub_success_count +=1
            if result['jindou_success']:
                jindou_success_count +=1
        log("  " + "-"*50)
    log("📊 总体统计:")
    log(f"  ├── 总账号数: {total_accounts}")
    log(f"  ├── 开源平台签到成功: {oshwhub_success_count}/{total_accounts}")
    log(f"  ├── 金豆签到成功: {jindou_success_count}/{total_accounts}")
    if total_points_reward>0:
        log(f"  ├── 总计获得积分: +{total_points_reward}")
    if total_jindou_reward>0:
        log(f"  ├── 总计获得金豆: +{total_jindou_reward}")
    oshwhub_rate = (oshwhub_success_count / total_accounts)*100 if total_accounts>0 else 0
    jindou_rate = (jindou_success_count / total_accounts)*100 if total_accounts>0 else 0
    log(f"  ├── 开源平台成功率: {oshwhub_rate:.1f}%")
    log(f"  └── 金豆签到成功率: {jindou_rate:.1f}%")
    failed_oshwhub = [r['account_index'] for r in all_results if not r['oshwhub_success'] and not r.get('password_error',False)]
    failed_jindou = [r['account_index'] for r in all_results if not r['jindou_success'] and not r.get('password_error',False)]
    if failed_oshwhub:
        log(f"  ⚠️ 开源平台失败账号: {', '.join(map(str,failed_oshwhub))}")
    if failed_jindou:
        log(f"  ⚠️ 金豆签到失败账号: {', '.join(map(str,failed_jindou))}")
    if password_error_accounts:
        log(f"  ⚠️密码错误的账号: {', '.join(map(str,password_error_accounts))}")
    if not failed_oshwhub and not failed_jindou and not password_error_accounts:
        log("  ✨ 所有账号全部签到成功!")
    elif password_error_accounts and not failed_oshwhub and not failed_jindou:
        log("  ✨除了密码错误账号，其他账号全部签到成功!")
    log("="*70)
    push_summary()
    all_failed_accounts = failed_accounts + password_error_accounts
    if enable_failure_exit and all_failed_accounts:
        log(f"⚠️ 检测到失败的账号: {', '.join(map(str,all_failed_accounts))}")
        if password_error_accounts:
            log(f"⚠️ 其中密码错误的账号: {', '.join(map(str,password_error_accounts))}")
        log("⚠️ 由于失败退出功能已开启，返回报错退出码以获得邮件提醒")
        sys.exit(1)
    else:
        if enable_failure_exit:
            log("✅ 所有账号签到成功，程序正常退出")
        else:
            log("✅ 程序正常退出")
        sys.exit(0)

if __name__ == "__main__":
    main()
