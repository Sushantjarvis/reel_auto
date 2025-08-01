#!/usr/bin/env python3
"""
auto_reel.py – Generate a Reel, upload it via Instagram’s **resumable-upload**
endpoint, then publish it.

REQUIRED ENV-VARS
-----------------
OPENAI_API_KEY    – your OpenAI key (images + TTS + GPT-4o)
IG_USER_ID        – numeric Instagram Business/Creator ID
IG_TOKEN          – long-lived access token with instagram_content_publish
"""

import os, sys, json, time, tempfile, logging, argparse, requests, uuid
from pytrends.request import TrendReq
from moviepy.editor import ImageClip, AudioFileClip, concatenate_videoclips
from dotenv import load_dotenv
import openai         # openai-python ≥1.30

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)

for var in ("OPENAI_API_KEY", "IG_USER_ID", "IG_TOKEN"):
    if not os.getenv(var):
        logging.error(f"Missing env-var {var}"); sys.exit(1)

openai.api_key = os.getenv("OPENAI_API_KEY")
IG_USER_ID     = os.getenv("IG_USER_ID")
IG_TOKEN       = os.getenv("IG_TOKEN")
GRAPH_ROOT     = "https://graph.facebook.com/v19.0"


# ──────────────────────────────────────────────────────────────────────────
#   1 ▸ Pick a hot topic
# ──────────────────────────────────────────────────────────────────────────
def trending(country: str = "IN") -> str:
    pt = TrendReq()
    pt.build_payload(kw_list=["news"], timeframe="now 1-H")
    topic = pt.trending_searches(pn=country).iat[0, 0]
    logging.info(f"Trending topic → {topic}")
    return topic


# ──────────────────────────────────────────────────────────────────────────
#   2 ▸ Write hook + caption + narration
# ──────────────────────────────────────────────────────────────────────────
PROMPT = """
Return valid JSON like:
{
 "hook": "≤12 words, no hashtags",
 "caption": "30-40 words, 3 emojis, 1 branded #YourBrand",
 "narr": "two sentences to be read aloud"
}
Topic: {topic}
"""

def script_for(topic: str) -> dict:
    rsp = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": PROMPT.format(topic=topic)}],
        response_format={"type": "json_object"},
        temperature=0.9,
    )
    return json.loads(rsp.choices[0].message.content)


# ──────────────────────────────────────────────────────────────────────────
#   3 ▸ Generate images & voice-over
# ──────────────────────────────────────────────────────────────────────────
def gen_images(prompt: str, n=4) -> list[str]:
    res = openai.images.generate(prompt=prompt, n=n, size="1080x1920", model="dall-e-3")
    paths = []
    for i, d in enumerate(res.data):
        p = tempfile.mktemp(suffix=f"_{i}.png")
        open(p, "wb").write(requests.get(d.url, timeout=30).content)
        paths.append(p)
    return paths

def tts(text: str) -> str:
    speech = openai.audio.speech.create(model="tts-1", voice="alloy",
                                        input=text, format="mp3")
    path = tempfile.mktemp(suffix=".mp3")
    speech.stream_to_file(path); return path


# ──────────────────────────────────────────────────────────────────────────
#   4 ▸ Assemble the vertical video
# ──────────────────────────────────────────────────────────────────────────
def build_video(imgs: list[str], audio: str) -> str:
    clips = [ImageClip(p).set_duration(3).resize(height=1920) for p in imgs]
    vid   = concatenate_videoclips(clips, method="compose")
    vid   = vid.set_audio(AudioFileClip(audio))
    out   = tempfile.mktemp(suffix=".mp4")
    vid.write_videofile(
        out, fps=30, codec="libx264", audio_codec="aac", logger=None, preset="medium"
    )
    return out


# ──────────────────────────────────────────────────────────────────────────
#   5 ▸ **Resumable upload** to Instagram (no S3 needed)
#      Docs: “Resumable Uploads – Instagram Platform” :contentReference[oaicite:0]{index=0}
# ──────────────────────────────────────────────────────────────────────────
def resumable_upload(mp4_path: str, caption: str) -> str:
    file_size = os.path.getsize(mp4_path)
    media_ep  = f"{GRAPH_ROOT}/{IG_USER_ID}/media"
    params    = {"access_token": IG_TOKEN}

    # ❶ start
    start = requests.post(
        media_ep,
        params=params,
        data={"media_type": "REELS", "upload_phase": "start", "file_size": file_size},
    ).json()
    session, vid_id = start["upload_session_id"], start["video_id"]
    start_off, end_off = int(start["start_offset"]), int(start["end_offset"])
    logging.info("Resumable session started")

    # ❷ transfer
    with open(mp4_path, "rb") as f:
        while start_off < end_off:
            f.seek(start_off)
            chunk = f.read(end_off - start_off)
            transfer = requests.post(
                media_ep,
                params=params,
                data={
                    "upload_phase": "transfer",
                    "upload_session_id": session,
                    "start_offset": start_off,
                },
                files={"video_file_chunk": chunk},
            ).json()
            start_off, end_off = int(transfer["start_offset"]), int(transfer["end_offset"])
    logging.info("Upload complete")

    # ❸ finish  → returns container (creation) ID
    finish = requests.post(
        media_ep,
        params=params,
        data={
            "upload_phase": "finish",
            "upload_session_id": session,
            "caption": caption,
        },
    ).json()
    container_id = finish["id"]
    logging.info(f"Container {container_id} created")
    return container_id


# ──────────────────────────────────────────────────────────────────────────
#   6 ▸ Publish!
# ──────────────────────────────────────────────────────────────────────────
def publish(container_id: str) -> str:
    res = requests.post(
        f"{GRAPH_ROOT}/{IG_USER_ID}/media_publish",
        params={"creation_id": container_id, "access_token": IG_TOKEN},
        timeout=60,
    ).json()
    if "id" not in res:
        raise RuntimeError(res)
    logging.info(f"✅ Reel published: {res['id']}")
    return res["id"]


# ──────────────────────────────────────────────────────────────────────────
#   Main entry-point
# ──────────────────────────────────────────────────────────────────────────
def main(topic_override: str | None):
    topic  = topic_override or trending()
    data   = script_for(topic)
    video  = build_video(gen_images(data["hook"]), tts(data["narr"]))
    cid    = resumable_upload(video, data["caption"])
    publish(cid)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", help="override trending topic")
    main(ap.parse_args().topic)
