#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import os
import pandas as pd
import requests
import json
import time
import subprocess
import traceback
import shutil
import re
import io

from urllib.parse import urlparse
from tqdm import tqdm

# Selenium 相关
from selenium.webdriver import Chrome, ChromeOptions
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException

# PDF 相关
from PyPDF2 import PdfMerger
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

# PyMuPDF (fitz) 相关
import fitz
import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Optional

# 新增：Selenium 其它用到的组件（这部分你原脚本是有的，但防止遗漏）
from selenium.webdriver.common.keys import Keys
import zipfile
import pdfplumber
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium import webdriver

# ------------------ 新增：HTML 清洗工具函数 ------------------
def remove_between_elsevier(html: str) -> str:
    """
    删除文本中第一个 'Elsevier' 到下一个 'Elsevier' 之间的内容（包含边界词），忽略大小写。
    如需删除所有成对片段，把 count=1 去掉即可。
    """
    pattern = re.compile(r'elsevier.*?elsevier', flags=re.IGNORECASE | re.DOTALL)
    cleaned = pattern.sub('', html, count=1)  # 只清一次；若想全删，去掉 count=1
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

@dataclass
class ProcessingResult:
    filename: str
    success: bool
    error_message: Optional[str] = None
    output_path: Optional[str] = None

class PDFProcessor:
    """
    用于提取 PDF 文本并进行简单的清洗，不使用多线程。
    """
    def __init__(self):
        self.setup_logging()
        
    def setup_logging(self):
        """Configure logging with rotation"""
        log_file = 'file_processing.log'
        self.logger = logging.getLogger('PDFProcessor')
        self.logger.setLevel(logging.INFO)
        
        # 清除已存在的 handlers，避免重复
        self.logger.handlers = []
        
        file_handler = RotatingFileHandler(
            log_file, maxBytes=5*1024*1024, backupCount=5, encoding='utf-8'
        )
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)

    def extract_text_from_pdf(self, pdf_path: str) -> str:
        """
        使用 PyMuPDF (fitz) 从 PDF 中提取文本，并用正则去除多余换行/空格。
        """
        text_parts = []
        try:
            doc = fitz.open(pdf_path)
            for page_num in range(len(doc)):
                page = doc[page_num]
                try:
                    text = page.get_text("text")
                    if text.strip():
                        # 将多余的换行符替换为空格
                        text = re.sub(r'\n+', ' ', text)
                        text_parts.append(text)
                except Exception as e:
                    self.logger.warning(f"Failed to extract text from page {page_num + 1}: {str(e)}")
                    continue
            
            doc.close()
            
            if not text_parts:
                raise ValueError("No text could be extracted from any page.")
            
            combined_text = ' '.join(text_parts)
            cleaned_text = re.sub(r'\s+', ' ', combined_text)
            return cleaned_text.strip()
            
        except Exception as e:
            raise ValueError(f"Failed to process PDF: {str(e)}")


# ------------------------------- 下面是合并后的主脚本逻辑 -------------------------------

user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.59 Safari/537.36"
max_hits_per_minute = 300  

# 读取 api_key
config_path = './refer/config.json' 
try:
    with open(config_path, 'r') as f:
        config = json.load(f)
    api_key = config.get('elsevierapikey')
    if not api_key:
        print("未在 config.json 中找到 elsevierapikey")
        exit()
except FileNotFoundError:
    print(f"配置文件未找到：{config_path}")
    exit()
except json.JSONDecodeError as e:
    print(f"读取配置文件错误：{e}")
    exit()

# ========== 2. 路径配置 ==========
parser = argparse.ArgumentParser(description='Elsevier Paper Crawler (with HTML->TXT cleaning)')
parser.add_argument('input_file', help='Input Excel file path')
args = parser.parse_args()
# 1) 读取 Excel
excel_path = args.input_file   # 使用命令行参数中的文件路径
df = pd.read_excel(excel_path)  # 读取Excel文件
# 2) 数据目录
download_dir = r'./langgragh/input'  

# 提取所需的列：文件名标签、DOI 和文章 URL
ccdc_codes = df.iloc[:, 0].tolist()  # 用于文件命名的标签
dois = df.iloc[:, 11].tolist()        # DOI 列
article_urls = df.iloc[:, 12].tolist() # 文章 URL 列

#1
# 定义用于处理支撑材料的函数
def unzip_file(zip_path, extract_to):
    """
    解压 ZIP 文件到指定目录，并处理潜在的嵌套 ZIP 文件。
    """
    if not zipfile.is_zipfile(zip_path):
        print(f"警告: '{zip_path}' 不是一个有效的 ZIP 文件。跳过解压。")
        return
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_to)
            print(f"成功解压 '{zip_path}' 到 '{extract_to}'。")
        
        # 处理解压后可能存在的嵌套 ZIP 文件
        for root, dirs, files in os.walk(extract_to):
            for file in files:
                if file.lower().endswith('.zip'):
                    nested_zip_path = os.path.join(root, file)
                    nested_extract_to = os.path.splitext(nested_zip_path)[0]
                    os.makedirs(nested_extract_to, exist_ok=True)
                    print(f"发现嵌套 ZIP 文件 '{nested_zip_path}'，正在解压到 '{nested_extract_to}'。")
                    unzip_file(nested_zip_path, nested_extract_to)
    except zipfile.BadZipFile:
        print(f"错误: '{zip_path}' 是一个损坏的 ZIP 文件。")
    except Exception as e:
        print(f"解压 '{zip_path}' 时发生意外错误: {e}")

def remove_else_file(full_path):
    allowed_extensions = {'.pdf', '.docx', '.doc'}

    for filename in os.listdir(full_path):
        file_path = os.path.join(full_path, filename)
        if os.path.isfile(file_path):
            _, extension = os.path.splitext(filename)
            if extension.lower() not in allowed_extensions:
                try:
                    os.remove(file_path)
                    print(f"已删除: {file_path}")
                except Exception as e:
                    print(f"删除 {file_path} 时出错: {e}")
#2
def download_supporting_materials(browser, full_path):
    # 从 xpath_patterns.json 文件中读取 dynamic_patterns
    with open(r'.../MOF_llm/referdemo/pathe.json', 'r', encoding='utf-8') as file:
        data = json.load(file)
        dynamic_patterns = data["dynamic_patterns"]  # 假设 dynamic_patterns 键始终存在

    # 遍历 dynamic_patterns
    for prefix, xpath in dynamic_patterns.items():
        try:
            # 根据 XPath 判断需要执行的操作
            if xpath.endswith('button'):
                action = 'click_button'
            elif xpath.endswith('a'):
                action = 'download_link'
            else:
                print(f"未知的 XPath 类型: {xpath}")
                continue

            # 找到所有匹配的元素
            elements = browser.find_elements(By.XPATH, xpath)
            print(f"找到 {len(elements)} 个匹配的元素，模式: {xpath}")

            # 遍历并处理每个元素
            for index, element in enumerate(elements, start=1):
                try:
                    if action == 'click_button':
                        print(f"尝试点击按钮 [{index}]: {xpath}")
                        browser.execute_script("(arguments[0]).click();", element)
                        time.sleep(10)  # 等待页面变化或下载

                    elif action == 'download_link':
                        print(f"尝试下载文件 [{index}]: {xpath}")
                        file_url = element.get_attribute('href')
                        if file_url:
                            print(f"文件下载链接: {file_url}")
                            response = requests.get(file_url, stream=True)
                            if response.status_code == 200:
                                filename = os.path.basename(file_url)
                                file_path = os.path.join(full_path, filename)
                                with open(file_path, 'wb') as f:
                                    for chunk in response.iter_content(chunk_size=8192):
                                        if chunk:
                                            f.write(chunk)
                                print(f"文件已成功下载: {file_path}")
                            else:
                                print(f"下载失败，HTTP 状态码: {response.status_code}")
                        else:
                            print(f"未找到下载链接: {xpath}")
                    else:
                        print(f"无法处理的操作类型: {action}")
                except Exception as e:
                    print(f"处理元素时发生错误: {e}")
        except Exception as e:
            print(f"未找到任何匹配的元素，模式: {xpath}，错误: {e}")

def start_libreoffice():#
    # 启动 LibreOffice（你可替换为实际绝对路径）
    subprocess.Popen([r'.../soffice.exe', '--headless', '--accept=socket,host=localhost,port=2002;urp;StarOffice.ComponentContext'])

def convert_to_pdf_with_unoconv(file_path):
    """使用 unoconv 将 .doc 和 .docx 文件转换为 PDF"""
    start_libreoffice()
    time.sleep(5)  # 等待 LibreOffice 启动
    os.environ['UNO_PATH'] = r'.../LibreOffice/program/'
    try:
        subprocess.run(['python', r'.../unoconv-master/unoconv-master/unoconv', '-f', 'pdf', file_path], check=True)
        print(f"已将 {file_path} 转换为 PDF")
    except subprocess.CalledProcessError as e:
        print(f"转换 {file_path} 为 PDF 时出错: {e}")

def pdf_to_html_with_pdfplumber(pdf_path):
    """使用 pdfplumber 将 PDF 转换为包含文本和表格的 HTML"""
    try:
        html_content = ""

        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                # 提取文本
                text = page.extract_text()
                if text:
                    text = text.replace('\n', '<br>')
                    html_content += f"<h2>Page {page_num + 1}</h2>\n"
                    html_content += f"<p>{text}</p>\n"

                # 提取表格
                tables = page.extract_tables()
                for table in tables:
                    df = pd.DataFrame(table[1:], columns=table[0])
                    html_content += df.to_html(index=False)
                    html_content += "<br>"

        return html_content
    except Exception as e:
        print(f"转换过程中出错: {e}")
        return ""

def convert_files_to_pdf(full_path):
    """将 .doc 和 .docx 文件转换为 PDF"""
    for filename in os.listdir(full_path):
        file_path = os.path.join(full_path, filename)
        if filename.lower().endswith(('.doc', '.docx')):
            # 使用 unoconv 将文件转换为 PDF
            convert_to_pdf_with_unoconv(file_path)

def convert_files_to_html(full_path):
    """将 PDF 文件转换为 HTML"""
    html_contents = []
    for filename in os.listdir(full_path):
        file_path = os.path.join(full_path, filename)
        if filename.lower().endswith('.pdf'):
            html_content = pdf_to_html_with_pdfplumber(file_path)
            html_contents.append(html_content)
    return "\n".join(html_contents)

def delete_non_html_files(full_path):
    """删除所有文件"""
    for filename in os.listdir(full_path):
        file_path = os.path.join(full_path, filename)
        if os.path.isfile(file_path):
            try:
                os.remove(file_path)
                print(f"已删除文件: {file_path}")
            except Exception as e:
                print(f"删除 {file_path} 时出错: {e}")

# 遍历每篇文献
for idx, (code, doi, url) in enumerate(zip(ccdc_codes, dois, article_urls)):
    print(f"正在处理第 {idx + 1} 篇文献，DOI: {doi}, URL: {url}, 文件名: {code}")

    # 下载正文部分
    doi = doi.strip()
    if not doi:
        print(f"文献 {code} 的 DOI 为空，跳过正文下载。")
        full_text_content = ""
    else:
        # 你需要提前初始化 ElsClient：client = ElsClient(config['elsevierapikey'])
        # 这里仅保留你原脚本的读取逻辑
        try:
            from elsapy.elsclient import ElsClient
            from elsapy.elsdoc import FullDoc
            # 加载 Elsevier key
            with open(r".../MOF_llm/referdemo/config.json") as con_file:
                cfg2 = json.load(con_file)
            client = ElsClient(cfg2['elsevierapikey'])
        except Exception as e:
            print(f"初始化 Elsevier 客户端出错：{e}")
            client = None

        if client:
            doc = FullDoc(doi=doi)
            if doc.read(client):
                full_text = None
                if 'full-text-retrieval-response' in doc.data:
                    ftrr = doc.data['full-text-retrieval-response']
                    if 'originalText' in ftrr:
                        full_text = ftrr['originalText']
                    elif 'originalTextHtml' in ftrr:
                        full_text = ftrr['originalTextHtml']
                elif 'originalText' in doc.data:
                    full_text = doc.data['originalText']
                elif 'originalTextHtml' in doc.data:
                    full_text = doc.data['originalTextHtml']

                if full_text:
                    if isinstance(full_text, dict):
                        if '$' in full_text:
                            full_text_content = full_text['$']
                        else:
                            full_text_content = json.dumps(full_text)
                            print(f"full_text 的结构不符合预期，已将其转换为 JSON 字符串。")
                    elif isinstance(full_text, str):
                        full_text_content = full_text
                    else:
                        full_text_content = str(full_text)
                    print(f"文献 {code} 的正文已成功获取。")
                else:
                    print(f"文献 {code} 的全文不可用。")
                    full_text_content = ""
            else:
                print(f"无法读取文献 {code} 的数据。可能是 DOI 无效。")
                full_text_content = ""
        else:
            full_text_content = ""

    # 处理支撑材料部分
    full_path = os.path.join(download_dir, code)
    if not os.path.exists(full_path):
        os.makedirs(full_path)
        print(f"已创建目录 '{code}'。")
    else:
        print(f"目录 '{code}' 已存在。")

    # 配置 Chrome 浏览器选项
    options = ChromeOptions()
    options.add_argument('--headless')  # 启用无头模式
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=20,10')  # 设置窗口大小
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_experimental_option('useAutomationExtension', False)
    user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.6668.59 Safari/537.36"
    options.add_argument(f'user-agent={user_agent}')
    prefs = {
        "download.default_directory": full_path,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "profile.default_content_setting_values.automatic_downloads": 1,
    }
    options.add_experimental_option('prefs', prefs)
    options.add_experimental_option('excludeSwitches', ['enable-logging'])  # 禁止日志打印

    # 指定 ChromeDriver 的路径（请替换为你的绝对路径）
    chrome_driver_path = r'.../chromedriver.exe'
    service = Service(executable_path=chrome_driver_path)

    # 初始化 Chrome 浏览器
    browser = webdriver.Chrome(service=service, options=options)
    browser.get(url)

    # 等待页面加载完成
    wait = WebDriverWait(browser, 20)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, 'body')))

    # 滚动页面以确保元素加载完毕
    for i in range(5):
        browser.find_element(By.TAG_NAME, 'body').send_keys(Keys.END)
        time.sleep(1)

    time.sleep(1)
    # 下载支撑材料
    download_supporting_materials(browser, full_path)
    browser.quit()

    # 解压下载的 zip 文件
    zip_files = [file for file in os.listdir(full_path) if file.lower().endswith('.zip')]
    if zip_files:
        for file in zip_files:
            zip_file_path = os.path.join(full_path, file)
            unzip_file(zip_file_path, full_path)
    else:
        print(f"在目录 '{full_path}' 中未找到 ZIP 文件。")

    # 删除非指定格式的文件
    remove_else_file(full_path)

    # 将 .doc 和 .docx 文件转换为 PDF
    convert_files_to_pdf(full_path)

    # 将所有 PDF 文件转换为 HTML 内容
    supporting_materials_html_content = convert_files_to_html(full_path)

    # 删除处理过程中的临时文件（PDF/Doc等）
    delete_non_html_files(full_path)
    shutil.rmtree(full_path)

    # 合并正文和支撑材料为 HTML
    combined_html_content = "<html><body>\n"
    combined_html_content += "<h1>Manuscript</h1>\n"
    combined_html_content += full_text_content
    combined_html_content += "\n<h1>Supplementary</h1>\n"
    combined_html_content += supporting_materials_html_content
    combined_html_content += "\n</body></html>"

    # 将合并后的内容保存为 HTML 文件
    filename = f"{download_dir}/{code}.html"
    with open(filename, "w", encoding="utf-8") as file:
        file.write(combined_html_content)
    print(f"合并后的 HTML 内容已保存到 {filename}")

    # ========== 新增：清洗 HTML -> 输出 TXT -> 删除 HTML ==========
    try:
        html_file_path = filename
        html_filename = os.path.basename(html_file_path)

        # 读取 HTML 文件内容
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()

        # 清洗（删除 'Elsevier' 到 'Elsevier' 之间的内容）
        cleaned_text = remove_between_elsevier(html_content)

        # 保存为 .txt（与 .html 同目录）
        txt_filename = html_filename.replace('.html', '.txt')
        txt_file_path = os.path.join(download_dir, txt_filename)
        with open(txt_file_path, "w", encoding="utf-8") as txt_file:
            txt_file.write(cleaned_text)
        print(f"清洗后的 TXT 内容已保存到 {txt_file_path}")
    except Exception as e:
        print(f"处理 HTML 文件 {html_filename} 时出错: {e}")

    # 删除 HTML 文件，只保留 TXT 文件
    try:
        os.remove(html_file_path)  # 删除生成的 HTML 文件
        print(f"已删除 HTML 文件: {html_file_path}")
    except Exception as e:
        print(f"删除 HTML 文件时出错: {e}")

    # 避免过于频繁的请求
    time.sleep(1)

print('下载和合并完成')

