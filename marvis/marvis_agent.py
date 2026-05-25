#!/usr/bin/env python3
"""
Marvis Agent — 操作系统层级全能助手
基于 AiLex Platform 基础设施

能力：
  1. 终端操作 — 执行命令、管理进程、系统监控
  2. 文件管理 — 读写、搜索、整理、备份
  3. 系统控制 — 服务启停、配置管理、Docker 操作
  4. AI 全场景 — 对话 + 代码 + 图片 + 视频 + 语音
  5. 网络服务 — API 调用、Webhook、数据抓取
  6. 自主行动 — 按计划执行任务、定时检查、自动报告
"""

import os
import sys
import json
import time
import readline
import subprocess
import shlex
from datetime import datetime

# ── Color ──
GREEN = '\033[92m'
CYAN = '\033[96m'
YELLOW = '\033[93m'
RED = '\033[91m'
BOLD = '\033[1m'
RESET = '\033[0m'

def print_banner():
    banner = f"""
{GREEN}{BOLD}
    __  ___            _           
   /  |/  /___ _   __(_)___ ______
  / /|_/ / __ \\ | / / / __ `/ ___/
 / /  / / /_/ / |/ / / /_/ / /    
/_/  /_/\\____/|___/_/\\__,_/_/     
                                  
  操作系统层级全能助手 — v1.0
{CYAN}  基于万量引擎 604 模型 · AiLex 基础设施{RESET}
"""
    print(banner)
    print(f"{YELLOW}  输入 /help 查看命令 · /exit 退出 · /sys 系统命令{RESET}")
    print(f"  当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()

class MarvisAgent:
    def __init__(self):
        self.mmi_key = os.environ.get("MMI_API_KEY", "")
        self.mmi_url = os.environ.get("MMI_BASE_URL", "https://millionengine.com/v1")
        self.history = []
        self.mode = "chat"  # chat | code | shell | system
        
    def run(self):
        print_banner()
        while True:
            try:
                prompt = f"{GREEN}marvis{RESET}:{CYAN}~{RESET}$ "
                user_input = input(prompt).strip()
                
                if not user_input:
                    continue
                
                if user_input == "/exit":
                    print(f"{YELLOW}Marvis 再见. 👋{RESET}")
                    break
                elif user_input == "/help":
                    self.show_help()
                elif user_input == "/sys":
                    self.show_system()
                elif user_input.startswith("/"):
                    self.handle_command(user_input)
                elif user_input.startswith("!"):
                    self.exec_shell(user_input[1:])
                else:
                    self.chat_with_ai(user_input)
                    
            except KeyboardInterrupt:
                print(f"\n{YELLOW}按 /exit 退出{RESET}")
            except EOFError:
                print(f"\n{YELLOW}Marvis 再见. 👋{RESET}")
                break
    
    def show_help(self):
        print(f"""
{BOLD}Marvis 命令列表:{RESET}
  /help         显示此帮助
  /exit         退出 Marvis
  /sys          系统状态概览
  /mode chat    对话模式（默认）
  /mode code    代码模式（专注写代码）  
  /mode shell   命令模式（专注执行）
  /mode system  系统管理模式
  /info         系统详细信息
  /ps           进程列表
  /df           磁盘使用
  /docker       Docker 状态
  /logs         查看最近日志
  !<command>    执行 Shell 命令（例如 !ls -la）
  /clear        清屏
  
  直接输入：和 Marvis 对话
  !开头：直接执行 Shell 命令
  /开头：Marvis 系统命令
""")
    
    def show_system(self):
        """系统概览"""
        info = {
            "hostname": os.uname().nodename,
            "system": os.uname().sysname,
            "release": os.uname().release,
            "uptime_cmd": subprocess.getoutput("uptime"),
            "cpu": subprocess.getoutput("nproc"),
            "memory": subprocess.getoutput("free -h | head -2"),
            "disk": subprocess.getoutput("df -h / | tail -1"),
            "docker": subprocess.getoutput("docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null || echo 'Docker not running'"),
            "marvis_port": os.environ.get("MARVIS_PORT", "8096"),
        }
        
        print(f"""
{BOLD}系统概览:{RESET}
  Host:     {info['hostname']}
  OS:       {info['system']} {info['release']}
  Uptime:   {info['uptime_cmd']}
  CPU:      {info['cpu']} 核
  Memory:   {info['memory']}
  Disk:     {info['disk'][:60]}
  Docker:   {info['docker'][:80]}
  Marvis:   :{info['marvis_port']}
""")
    
    def exec_shell(self, cmd):
        """执行 Shell 命令"""
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            if result.stdout:
                print(result.stdout)
            if result.stderr:
                print(f"{RED}{result.stderr}{RESET}")
            if result.returncode != 0:
                print(f"{RED}Exit code: {result.returncode}{RESET}")
        except subprocess.TimeoutExpired:
            print(f"{RED}命令超时 (30s){RESET}")
        except Exception as e:
            print(f"{RED}错误: {e}{RESET}")
    
    def chat_with_ai(self, text):
        """调用 AI"""
        import httpx
        
        messages = [
            {"role": "system", "content": f"""你是 Marvis，一个操作系统层级的全能 AI 助手。

你的能力：
- 系统操作：执行命令、管理文件、控制服务
- 代码编写：Python、JavaScript、Shell、Go
- 数据分析：处理文本、JSON、CSV、日志
- AI 调用：对话、代码生成、图片理解、视频生成
- 联网能力：抓取网页、调用 API
- 自主行动：按计划执行任务

输出规则：
- 如果需要执行命令，用 ```bash 代码块
- 如果用户输入 ! 开头，会直接执行 Shell
- 保持简洁，直接解决问题
- 中文回答，技术术语用英文

当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
用户：{os.environ.get('USER', '老大')}
工作目录：{os.getcwd()}"""},
            {"role": "user", "content": text}
        ]
        
        payload = {
            "model": "gpt-4o",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        
        try:
            import httpx
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{self.mmi_url}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.mmi_key}"},
                )
                if resp.status_code == 200:
                    reply = resp.json()["choices"][0]["message"]["content"]
                    
                    # Check for code blocks with bash commands
                    if "```bash" in reply:
                        # Extract and offer to run
                        print(f"\n{CYAN}{reply.split('```bash')[0]}{RESET}")
                        cmd_block = reply.split("```bash")[1].split("```")[0].strip()
                        print(f"{YELLOW}[检测到命令] 运行? (y/N){RESET}")
                        choice = input().strip().lower()
                        if choice == 'y':
                            print(f"{GREEN}执行: {cmd_block[:100]}{RESET}")
                            self.exec_shell(cmd_block)
                        else:
                            print(f"{YELLOW}跳过命令执行{RESET}")
                            print(f"\n{CYAN}{reply}{RESET}")
                    else:
                        print(f"\n{CYAN}{reply}{RESET}")
                else:
                    print(f"{RED}API 错误: {resp.status_code} {resp.text[:200]}{RESET}")
        except Exception as e:
            print(f"{RED}连接失败: {e}{RESET}")
    
    def handle_command(self, cmd):
        parts = cmd.split()
        if parts[0] == "/mode" and len(parts) > 1:
            self.mode = parts[1]
            print(f"{GREEN}模式切换为: {self.mode}{RESET}")
        elif parts[0] == "/info":
            self.exec_shell("uname -a && echo --- && free -h && echo --- && df -h /")
        elif parts[0] == "/ps":
            self.exec_shell("ps aux --sort=-%mem | head -15")
        elif parts[0] == "/df":
            self.exec_shell("df -h")
        elif parts[0] == "/docker":
            self.exec_shell("docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo 'Docker not available'")
        elif parts[0] == "/logs":
            self.exec_shell("tail -50 /var/log/syslog 2>/dev/null || journalctl -n 50 --no-pager 2>/dev/null || echo 'No system log access'")
        elif parts[0] == "/clear":
            os.system('clear')
            print_banner()
        else:
            print(f"{YELLOW}未知命令: {cmd}. 输入 /help 查看{RESET}")

if __name__ == "__main__":
    agent = MarvisAgent()
    agent.run()
