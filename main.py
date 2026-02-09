import os
import json
import subprocess
import tempfile

from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)

# =========================
# Config
# =========================
TARGET_W = 1080
TARGET_H = 1920

VF_916 = (
    f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=decrease,"
    f"pad={TARGET_W}:{TARGET_H}:(ow-iw)/2:(oh-ih)/2"
)

# =========================
# Basic endpoints
# =========================
@app.get("/")
def root():
    return "ffmpeg-renderer up", 200


@app.get("/health")
def health():
    return "ok", 200


# =========================
# Helpers
# =========================
def run_cmd(cmd: list[str]):
    try:
        subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "Command failed:\n"
            f"{' '.join(cmd)}\n\n"
            f"OUTPUT:\n{e.output}"
        ) from e


def ffprobe_duration_sec(path: str) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ], text=True).strip()
    return float(out)


def normalize_scene(video_in: str, video_out: str, target_sec: float, fps: int) -> float:
    """
    씬 mp4 처리:
    - target_sec에 맞춰 Trim 또는 Pad/Loop
    - 9:16 + fps 통일
    """
    actual = ffprobe_duration_sec(video_in)
    vf_base = f"{VF_916},fps={fps}"

    tol = 0.03
    SMALL_PAD_SEC = 0.5

    # 길거나 거의 같으면 Trim
    if abs(actual - target_sec) <= tol or actual > target_sec:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            video_out
        ])
        return target_sec

    # 짧으면 Pad / Loop
    short_by = target_sec - actual

    if short_by <= SMALL_PAD_SEC:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", f"{vf_base},tpad=stop_mode=clone:stop_duration={short_by:.3f}",
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            video_out
        ])
        return target_sec

    run_cmd([
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", video_in,
        "-vf", vf_base,
        "-t", f"{target_sec:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-an",
        video_out
    ])
    return target_sec


def cut_audio_segment_to_aac(
    audio_in: str,
    audio_out: str,
    start_sec: float,
    dur_sec: float,
    pad_to_sec: float | None = None
):
    """
    audio_in에서 start_sec부터 dur_sec만큼 잘라 AAC(m4a)로 저장.
    - pad_to_sec가 주어지면, apad로 무음 패딩 후 길이 고정.
    """
    if dur_sec <= 0:
        dur_sec = 0.001

    if pad_to_sec is None:
        run_cmd([
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}",
            "-t", f"{dur_sec:.3f}",
            "-i", audio_in,
            "-c:a", "aac",
            "-b:a", "192k",
            audio_out
        ])
        return

    # pad_to_sec까지 무음 패딩
    tail = max(0.0, pad_to_sec - dur_sec)
    run_cmd([
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-t", f"{dur_sec:.3f}",
        "-i", audio_in,
        "-af", f"apad=pad_dur={tail:.3f}",
        "-t", f"{pad_to_sec:.3f}",
        "-c:a", "aac",
        "-b:a", "192k",
        audio_out
    ])


def pad_video_tail(video_in: str, video_out: str, extra_sec: float, fps: int) -> float:
    """
    video_in 뒤에 extra_sec 만큼 마지막 프레임 복제(tpad)로 여운 추가.
    """
    if extra_sec <= 0:
        run_cmd(["ffmpeg", "-y", "-i", video_in, "-c", "copy", video_out])
        return ffprobe_duration_sec(video_out)

    run_cmd([
        "ffmpeg", "-y",
        "-i", video_in,
        "-vf", f"fps={fps},tpad=stop_mode=clone:stop_duration={extra_sec:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-an",
        video_out
    ])
    return ffprobe_duration_sec(video_out)


def mux_video_audio(video_in: str, audio_in: str, out_mp4: str, fps: int):
    run_cmd([
        "ffmpeg", "-y",
        "-i", video_in,
        "-i", audio_in,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-r", str(fps),
        "-shortest",
        out_mp4
    ])


# =========================
# Render endpoint
# =========================
@app.post("/render")
def render():
    try:
        data = request.get_json(force=True, silent=True)
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        audio = data.get("audio")
        videos = data.get("videos")
        output = data.get("output")
        durations_sec = data.get("durations_sec")
        fps = int(data.get("fps", 30))

        # 마지막 여운(영상만 늘림). 기본 2초
        tail_extra_sec = float(data.get("tail_extra_sec", 2.0))

        # 마지막 씬에서 남은 오디오를 "전부" 가져올지 (기본 True)
        last_audio_take_rest = bool(data.get("last_audio_take_rest", True))

        # 하위호환
        if durations_sec is None:
            durations_ms = data.get("durations_ms")
            if durations_ms is not None:
                durations_sec = [float(x) / 1000.0 for x in durations_ms]

        if not audio or not videos or not output:
            return jsonify({"ok": False, "error": "payload missing fields"}), 400
        if not isinstance(videos, list):
            return jsonify({"ok": False, "error": "videos must be array"}), 400
        if durations_sec is None:
            return jsonify({"ok": False, "error": "payload missing durations_sec (or durations_ms)"}), 400
        if len(durations_sec) != len(videos):
            return jsonify({"ok": False, "error": "length mismatch"}), 400

        # 문자열 섞여 와도 안전 처리
        durations_sec = [float(x) for x in durations_sec]

        client = storage.Client()

        def download_gs(gs_path: str, local_path: str):
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            bucket.blob(blob_name).download_to_filename(local_path)

        def upload_gs(local_path: str, gs_path: str):
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            bucket.blob(blob_name).upload_from_filename(local_path, content_type="video/mp4")

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) Download Audio
            audio_path = os.path.join(tmpdir, "audio.mp3")
            download_gs(audio, audio_path)
            audio_total_sec = ffprobe_duration_sec(audio_path)

            seg_paths = []
            debug_scenes = []

            cur_start = 0.0
            sum_script = sum(durations_sec)

            for i, (gs_url, dur) in enumerate(zip(videos, durations_sec)):
                target_sec = float(dur)
                is_last = (i == len(videos) - 1)

                raw_vp = os.path.join(tmpdir, f"video_raw_{i}.mp4")
                download_gs(gs_url, raw_vp)

                # 2) 비디오를 target에 맞춤
                fixed_vp = os.path.join(tmpdir, f"video_fixed_{i}.mp4")
                video_sec = normalize_scene(raw_vp, fixed_vp, target_sec, fps)

                # 마지막이면 tail만큼 영상 늘림(오디오는 그대로)
                if is_last and tail_extra_sec > 0:
                    fixed_tail = os.path.join(tmpdir, f"video_fixed_tail_{i}.mp4")
                    video_sec = pad_video_tail(fixed_vp, fixed_tail, tail_extra_sec, fps)
                    fixed_vp = fixed_tail

                # 3) 오디오 구간 계산
                remaining = max(0.0, audio_total_sec - cur_start)

                if is_last and last_audio_take_rest:
                    # 마지막 씬은 남은 오디오를 전부 가져오기 (오디오 절대 잘림 방지)
                    audio_seg_sec = remaining
                else:
                    audio_seg_sec = min(target_sec, remaining)

                audio_seg = os.path.join(tmpdir, f"audio_seg_{i}.m4a")

                # 마지막 씬: 영상이 더 길면 무음 패딩해서 video_sec 길이로 맞춤
                if is_last:
                    cut_audio_segment_to_aac(
                        audio_in=audio_path,
                        audio_out=audio_seg,
                        start_sec=cur_start,
                        dur_sec=audio_seg_sec,
                        pad_to_sec=video_sec
                    )
                    note = "Last: took rest audio; padded to video with silence"
                else:
                    cut_audio_segment_to_aac(
                        audio_in=audio_path,
                        audio_out=audio_seg,
                        start_sec=cur_start,
                        dur_sec=audio_seg_sec,
                        pad_to_sec=None
                    )
                    note = "Normal: cut audio to target"

                seg_out = os.path.join(tmpdir, f"seg_{i}.mp4")
                mux_video_audio(fixed_vp, audio_seg, seg_out, fps)
                seg_paths.append(seg_out)

                debug_scenes.append({
                    "idx": i,
                    "is_last": is_last,
                    "start_sec": round(cur_start, 3),
                    "target_script_sec": round(target_sec, 3),
                    "audio_cut_sec": round(audio_seg_sec, 3),
                    "video_final_sec": round(video_sec, 3),
                    "note": note
                })

                # 컷 전환 기준 타임라인 이동
                cur_start += target_sec

            # 4) Concat segments
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for vp in seg_paths:
                    f.write(f"file '{vp}'\n")

            final_video = os.path.join(tmpdir, "final.mp4")
            run_cmd([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                final_video
            ])

            upload_gs(final_video, output)

        return jsonify({
            "ok": True,
            "output": output,
            "videoCount": len(videos),
            "audio_total_sec": round(audio_total_sec, 3),
            "sum_script_sec": round(sum_script, 3),
            "tail_extra_sec": round(tail_extra_sec, 3),
            "last_audio_take_rest": last_audio_take_rest,
            "debug": debug_scenes
        }), 200

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "internal error",
            "detail": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
