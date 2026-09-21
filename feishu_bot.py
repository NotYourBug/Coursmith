# -*- coding: utf-8 -*-
"""
飞书课程生成机器人（独立程序 #3）
用法：python feishu_bot.py
功能：通过飞书机器人接收结构化消息，自动生成课程。

前提条件：
1. 在飞书开发者后台创建企业自建应用，开启"机器人"能力
2. 在 config.json 中配置 feishu_app_id，feishu_app_secret，
   feishu_encrypt_key，feishu_verification_token
3. pip install lark-oapi>=1.6.0
"""

import re
import os
import json
import threading
import sys

from course_gen_core import (
    load_config, get_config,
    run_full_pipeline_for_titles,
    check_and_clean_incomplete_courses, run_post_pipeline,
    sanitize_filename,
)


def parse_course_message(text):
    """
    解析飞书消息。
    返回 (lessons_per_course, topics_dict)。
    topics_dict = {"主题名": [课程标题列表], ...}
    """
    text = text.strip()
    lessons = 30
    topics = {}
    current_topic = None

    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue

        m = re.match(r'每门节数\s*[:：]\s*(\d+)', line)
        if m:
            lessons = int(m.group(1))
            continue

        m = re.match(r'主题\s*[:：]\s*(.+)', line)
        if m:
            current_topic = m.group(1).strip()
            if current_topic not in topics:
                topics[current_topic] = []
            continue

        m = re.findall(r'《([^》]+)》', line)
        if m and current_topic:
            for title in m:
                t = title.strip()
                if t and t not in topics[current_topic]:
                    topics[current_topic].append(t)

    if not topics:
        return None, {}
    return lessons, topics


def process_feishu_request(lessons, topics, footer_text, thread_num):
    """处理飞书请求：每个主题下生成课程 -> 检查 -> 后处理。"""
    input_dir = "课程标题"
    os.makedirs(input_dir, exist_ok=True)
    results = {}

    for topic_name, course_titles in topics.items():
        if not course_titles:
            continue
        topic_dir = os.path.join(input_dir, sanitize_filename(topic_name))
        os.makedirs(topic_dir, exist_ok=True)
        original_cwd = os.getcwd()
        os.chdir(topic_dir)
        try:
            print(f"\n===== 主题: {topic_name} ({len(course_titles)}门课, 每门{lessons}节) =====")
            run_full_pipeline_for_titles(
                course_titles=course_titles,
                lessons_per_course=lessons,
                footer_text=footer_text,
                thread_num=thread_num,
                do_png=False,
                do_zip=False,
            )
            results[topic_name] = {"status": "ok", "count": len(course_titles)}
        except Exception as e:
            print(f"主题 {topic_name} 处理失败: {e}")
            results[topic_name] = {"status": "error", "error": str(e)}
        finally:
            os.chdir(original_cwd)

    print("\n===== 完整性检查 =====")
    passed, deleted = check_and_clean_incomplete_courses(input_dir, lessons)

    print("\n===== 后处理（ZIP + PNG）=====")
    for topic_name in topics:
        topic_dir = os.path.join(input_dir, sanitize_filename(topic_name))
        if os.path.isdir(topic_dir):
            original_cwd = os.getcwd()
            os.chdir(topic_dir)
            try:
                run_post_pipeline(None, do_png=True, do_zip=True, png_workers=10)
            finally:
                os.chdir(original_cwd)

    return results, passed, deleted


def _send_reply(api_client, sender_id, text):
    """通过飞书 API 发送回复消息。"""
    try:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        api_client.im.v1.message.create(CreateMessageRequest(
            receive_id=sender_id,
            params={"receive_id_type": "open_id"},
            body=CreateMessageRequestBody(
                content=json.dumps({"text": text}), msg_type="text"
            ),
        ))
    except Exception as e:
        print(f"发送回复失败: {e}")


def _read_cfg_str(key, default=""):
    try:
        with open("config.json", 'r', encoding='utf-8') as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


def run_feishu_bot():
    """启动飞书机器人 WebSocket 长连接。"""
    load_config()
    cfg = get_config()
    app_id = cfg.get("feishu_app_id", "")
    app_secret = cfg.get("feishu_app_secret", "")
    encrypt_key = _read_cfg_str("feishu_encrypt_key", "")
    verification_token = _read_cfg_str("feishu_verification_token", "")

    if not app_id or not app_secret:
        print("=" * 60)
        print("飞书 Bot 未配置！请按以下步骤操作：")
        print("1. 访问 https://open.feishu.cn/ 创建企业自建应用")
        print("2. 在应用功能中开启「机器人」能力")
        print("3. 获取 App ID 和 App Secret")
        print("4. 在 config.json 中填写 feishu_app_id 和 feishu_app_secret")
        print("5. 在事件订阅页面获取 Encrypt Key 和 Verification Token")
        print("6. 在 config.json 中填写 feishu_encrypt_key 和 feishu_verification_token")
        print("7. 重新运行 python feishu_bot.py")
        print("=" * 60)
        return

    if not encrypt_key or not verification_token:
        print("=" * 60)
        print("飞书事件订阅未配置！")
        print("请在 Feishu 应用的事件订阅页面获取 Encrypt Key 和 Verification Token")
        print("然后在 config.json 中填写 feishu_encrypt_key 和 feishu_verification_token")
        print("=" * 60)
        return

    footer_text = "资料云集"
    thread_num = 100

    from lark_oapi import EventDispatcherHandler, Client as LarkClient
    from lark_oapi.ws import Client as WsClient
    from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

    api_client = LarkClient.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .build()

    def on_message(message_text, sender_id):
        print(f"\n收到消息 (来自 {sender_id}):")
        print(message_text[:200])
        lessons, topics = parse_course_message(message_text)
        if not topics:
            reply = "未能解析课程信息，请检查格式。\n支持格式：\n每门节数:x\n主题：xxx\n《课程标题》"
            print("解析失败")
            _send_reply(api_client, sender_id, reply)
            return
        total = sum(len(v) for v in topics.values())
        threading.Thread(target=process_feishu_request,
                         args=(lessons, topics, footer_text, thread_num), daemon=True).start()
        reply = f"收到！共 {len(topics)} 个主题、{total} 门课程，每门 {lessons} 节。正在后台生成..."
        _send_reply(api_client, sender_id, reply)

    def handle_im_message(event: P2ImMessageReceiveV1):
        msg = event.event.message
        if msg.message_type != "text":
            return
        content_str = msg.content
        try:
            text = json.loads(content_str).get("text", "")
        except Exception:
            text = content_str
        sender_id = event.event.sender.sender_id
        on_message(text, sender_id)

    handler = EventDispatcherHandler.builder(encrypt_key, verification_token) \
        .register_p2_im_message_receive_v1(handle_im_message) \
        .build()

    ws_client = WsClient(
        app_id=app_id,
        app_secret=app_secret,
        event_handler=handler,
    )

    print(f"飞书 Bot 启动中... App ID: {app_id[:8]}***")
    print("等待飞书消息... 消息格式: 每门节数:x / 主题：xxx / 《课程标题》")
    print("Ctrl+C 退出")
    ws_client.start()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-parse":
        test_text = """每门节数:25
主题：Python基础
《Python入门》
《面向对象编程》

主题：数据分析
《Pandas实战》
《NumPy基础》"""
        lessons, topics = parse_course_message(test_text)
        print(f"节数: {lessons}")
        for topic, titles in topics.items():
            print(f"  {topic}: {titles}")
    else:
        run_feishu_bot()
