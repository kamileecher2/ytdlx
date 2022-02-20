#!/usr/local/bin/python3
# coding: utf-8

# ytdlbot - downloader.py
# 8/14/21 16:53
#

__author__ = "Benny <benny.think@gmail.com>"

import logging
import os
import pathlib
import random
import re
import subprocess
import time
from io import StringIO
from unittest.mock import MagicMock

import fakeredis
import ffmpeg
import filetype
import yt_dlp as ytdl
from tqdm import tqdm
from yt_dlp import DownloadError

from config import AUDIO_FORMAT, ENABLE_VIP, MAX_DURATION, TG_MAX_SIZE
from db import Redis
from limit import VIP
from utils import (adjust_formats, apply_log_formatter, current_time,
                   get_user_settings)

r = fakeredis.FakeStrictRedis()
apply_log_formatter()


def sizeof_fmt(num: int, suffix='B'):
    for unit in ['', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi']:
        if abs(num) < 1024.0:
            return "%3.1f%s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f%s%s" % (num, 'Yi', suffix)


def edit_text(bot_msg, text):
    key = f"{bot_msg.chat.id}-{bot_msg.message_id}"
    # if the key exists, we shouldn't send edit message
    if not r.exists(key):
        time.sleep(random.random())
        r.set(key, "ok", ex=3)
        bot_msg.edit_text(text)


def tqdm_progress(desc, total, finished, speed="", eta=""):
    def more(title, initial):
        if initial:
            return f"{title} {initial}"
        else:
            return ""

    f = StringIO()
    tqdm(total=total, initial=finished, file=f, ascii=False, unit_scale=True, ncols=30,
         bar_format="{l_bar}{bar} |{n_fmt}/{total_fmt} "
         )
    raw_output = f.getvalue()
    tqdm_output = raw_output.split("|")
    progress = f"`[{tqdm_output[1]}]`"
    detail = tqdm_output[2]
    text = f"""
{desc}

{more("🚦 Ilerleme:", progress)}
{more("🔻 Indirilen:", detail)}
{more("⚡️ Hız:", speed)}
{more("⏰ Zaman:", eta)}

    """
    f.close()
    return text


def remove_bash_color(text):
    return re.sub(r'\u001b|\[0;94m|\u001b\[0m|\[0;32m|\[0m|\[0;33m', "", text)


def download_hook(d: dict, bot_msg):
    # since we're using celery, server location may be located in different continent.
    # Therefore, we can't trigger the hook very often.
    # the key is user_id + download_link
    original_url = d["info_dict"]["original_url"]
    key = f"{bot_msg.chat.id}-{original_url}"

    if d['status'] == 'downloading':
        downloaded = d.get("downloaded_bytes", 0)
        total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)

        # percent = remove_bash_color(d.get("_percent_str", "N/A"))
        speed = remove_bash_color(d.get("_speed_str", "N/A"))
        if ENABLE_VIP and not r.exists(key):
            result, err_msg = check_quota(total, bot_msg.chat.id)
            if result is False:
                raise ValueError(err_msg)
        eta = remove_bash_color(d.get("_eta_str", d.get("eta")))
        text = tqdm_progress("⏬ Indiriliyor...", total, downloaded, speed, eta)
        edit_text(bot_msg, text)
        r.set(key, "ok", ex=5)


def upload_hook(current, total, bot_msg):
    # filesize = sizeof_fmt(total)
    text = tqdm_progress("⏫ Yukleniyor...", total, current)
    edit_text(bot_msg, text)


def check_quota(file_size, chat_id) -> ("bool", "str"):
    remain, _, ttl = VIP().check_remaining_quota(chat_id)
    if file_size > remain:
        refresh_time = current_time(ttl + time.time())
        err = f"Quota exceed, you have {sizeof_fmt(remain)} remaining, " \
              f"but you want to download a video with {sizeof_fmt(file_size)} in size. \n" \
              f"Try again in {ttl} seconds({refresh_time})"
        logging.warning(err)
        Redis().update_metrics("quota_exceed")
        return False, err
    else:
        return True, ""


def convert_to_mp4(resp: dict, bot_msg):
    default_type = ["video/x-flv", "video/webm"]
    if resp["status"]:
        # all_converted = []
        for path in resp["filepath"]:
            # if we can't guess file type, we assume it's video/mp4
            mime = getattr(filetype.guess(path), "mime", "video/mp4")
            if mime in default_type:
                if not can_convert_mp4(path, bot_msg.chat.id):
                    logging.warning("Conversion abort for non VIP %s", bot_msg.chat.id)
                    bot_msg._client.send_message(
                        bot_msg.chat.id,
                        "You're not VIP, so you can't convert longer video to streaming formats.")
                    break
                edit_text(bot_msg, f"{current_time()}: Video {path.name} mp4'e Donuşturuluyor. Lütfen bekle.")
                new_file_path = path.with_suffix(".mp4")
                logging.info("Detected %s, converting to mp4...", mime)
                subprocess.check_output(["ffmpeg", "-y", "-i", path, new_file_path])
                index = resp["filepath"].index(path)
                resp["filepath"][index] = new_file_path

        return resp

