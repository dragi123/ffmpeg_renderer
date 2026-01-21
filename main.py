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
TAIL_SEC = 2.0  # 오디오 끝난 뒤 영상 여유(초)

# 9:16 캔버스 강제(왜곡 없이)
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
    """Run subprocess and raise detailed error output if fails."""
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


def normalize_scene(video_in: str, video_out: str, target_sec: float, fps: int):
    """
    씬 mp4를 target_sec로 맞춘다.
    - 길면: trim(-t)
    - 짧으면:
        * 아주 조금 부족(<= SMALL_PAD_SEC): 마지막 프레임 미세 pad(tpad clone)
        * 많이 부족: loop로 채운 뒤 target_sec로 컷
    """
    actual = ffprobe_duration_sec(video_in)

    # 로그(원인 파악에 도움)
    # print(f"[scene] in={os.path.basename(video_in)} actual={actual:.3f}s target={target_sec:.3f}s short_by={target_sec-actual:.3f}s", flush=True)

    tol = 0.03  # 30ms
    SMALL_PAD_SEC = 0.5  # 이 정도까지만 정지 프레임 허용(취향대로 0.3~0.7)

    # 공통 필터: 9:16 + fps 통일
    vf_base = f"{VF_916},fps={fps}"

    # 거의 동일한 길이라도 안정성을 위해 재인코딩하며 target로 컷
    if abs(actual - target_sec) <= tol:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            video_out
        ])
        return actual

    if actual > target_sec:
        # Trim
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            video_out
        ])
        return actual

    # 여기부터 actual < target
    short_by = target_sec - actual

    if short_by <= SMALL_PAD_SEC:
        # 아주 조금 부족한 건 정지로 미세 보정 (루프보다 자연스러움)
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", f"{vf_base},tpad=stop_mode=clone:stop_duration={short_by:.3f}",
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            video_out
        ])
        return actual

    # 많이 부족하면: 무한 루프 입력 후 target_sec로 컷
    run_cmd([
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", video_in,
        "-vf", vf_base,
        "-t", f"{target_sec:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",
        video_out
    ])
    return actual


def normalize_total_to_audio(merged_in: str, merged_out: str, audio_path: str, fps: int):
    """
    concat된 merged 영상을 (오디오 길이 + TAIL_SEC)에 맞춘다.
    - merged가 길면 trim
    - 짧으면 pad
    """
    audio_sec = ffprobe_duration_sec(audio_path)
    target_total = audio_sec + TAIL_SEC

    merged_sec = ffprobe_duration_sec(merged_in)

    if merged_sec >= target_total:
        run_cmd([
            "ffmpeg", "-y", "-i", merged_in,
            "-t", f"{target_total:.3f}",
            "-r", str(fps),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            merged_out
        ])
    else:
        pad_sec = target_total - merged_sec
        run_cmd([
            "ffmpeg", "-y", "-i", merged_in,
            "-vf", f"tpad=stop_mode=clone:stop_duration={pad_sec:.3f}",
            "-t", f"{target_total:.3f}",
            "-r", str(fps),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            merged_out
        ])

    return {
        "audio_sec": round(audio_sec, 3),
        "target_total_sec": round(target_total, 3),
        "merged_sec": round(merged_sec, 3),
    }


# =========================
# Render endpoint
# =========================
@app.post("/render")
def render():
    try:
        data = request.get_json(force=True, silent=True)

        # n8n stringified JSON 대비
        if isinstance(data, str):
            data = json.loads(data)

        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        audio = data.get("audio")
        videos = data.get("videos")
        output = data.get("output")
        durations_ms = data.get("durations_ms")
        fps = int(data.get("fps", 30))

        if not audio or not videos or not output:
            return jsonify({
                "ok": False,
                "error": "payload must include audio, videos[], output"
            }), 400

        if not isinstance(videos, list):
            return jsonify({"ok": False, "error": "videos must be an array"}), 400

        if not durations_ms or not isinstance(durations_ms, list):
            return jsonify({"ok": False, "error": "payload must include durations_ms[]"}), 400

        if len(durations_ms) != len(videos):
            return jsonify({
                "ok": False,
                "error": f"length mismatch: videos={len(videos)} durations_ms={len(durations_ms)}"
            }), 400

        # GCS client
        client = storage.Client()

        def download_gs(gs_path: str, local_path: str):
            if not gs_path.startswith("gs://"):
                raise ValueError(f"Invalid GCS path: {gs_path}")
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.download_to_filename(local_path)

        def upload_gs(local_path: str, gs_path: str):
            if not gs_path.startswith("gs://"):
                raise ValueError(f"Invalid GCS path: {gs_path}")
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(local_path, content_type="video/mp4")

        with tempfile.TemporaryDirectory() as tmpdir:
            # 1) Download audio
            audio_path = os.path.join(tmpdir, "audio.mp3")
            download_gs(audio, audio_path)

            # 2) Download raw videos
            raw_video_paths = []
            for i, v in enumerate(videos):
                vp = os.path.join(tmpdir, f"video_raw_{i}.mp4")
                download_gs(v, vp)
                raw_video_paths.append(vp)

            # 3) Normalize each scene by durations_ms
            fixed_video_paths = []
            debug_scenes = []

            for i, (vp, dms) in enumerate(zip(raw_video_paths, durations_ms)):
                target_sec = float(dms) / 1000.0
                fixed_vp = os.path.join(tmpdir, f"video_fixed_{i}.mp4")

                raw_sec = normalize_scene(vp, fixed_vp, target_sec, fps=fps)

                fixed_video_paths.append(fixed_vp)
                debug_scenes.append({
                    "idx": i,
                    "target_sec": round(target_sec, 3),
                    "raw_sec": round(raw_sec, 3),
                    "short_by": round(target_sec - raw_sec, 3),
                    "video": videos[i],
                })

            # 4) Concat fixed scenes
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for vp in fixed_video_paths:
                    f.write(f"file '{vp}'\n")

            merged_video = os.path.join(tmpdir, "merged.mp4")
            run_cmd([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-r", str(fps),
                "-an",
                merged_video
            ])

            # 5) Ensure total length = audio + 2s
            merged_fixed = os.path.join(tmpdir, "merged_fixed.mp4")
            debug_total = normalize_total_to_audio(merged_video, merged_fixed, audio_path, fps=fps)

            # 6) Mux audio only (keep video length; no -shortest to preserve +2s tail)
            final_video = os.path.join(tmpdir, "final.mp4")
            run_cmd([
                "ffmpeg", "-y",
                "-i", merged_fixed,
                "-i", audio_path,
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                final_video
            ])

            # 7) Upload
            upload_gs(final_video, output)

        return jsonify({
            "ok": True,
            "output": output,
            "videoCount": len(videos),
            "fps": fps,
            "debug": {
                "scenes": debug_scenes,
                "total": debug_total,
                "note": "final length is audio + TAIL_SEC; video is 9:16 normalized"
            }
        }), 200

    except Exception as e:
        return jsonify({
            "ok": False,
            "error": "internal error",
            "detail": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
