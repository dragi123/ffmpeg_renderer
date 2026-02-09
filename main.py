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

# 9:16 캔버스 강제 (마지막 씬 포함 모든 씬에 적용)
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


def normalize_scene(video_in: str, video_out: str, target_sec: float, fps: int, keep_original_duration: bool = False):
    """
    씬 mp4 처리 함수
    - keep_original_duration=True (마지막 씬용): 길이 자르지 않고 포맷(9:16, fps)만 변환
    - False (일반 씬): target_sec에 맞춰서 Trim 또는 Loop/Pad
    """
    actual = ffprobe_duration_sec(video_in)
    
    # 공통 필터: 9:16 + fps 통일
    vf_base = f"{VF_916},fps={fps}"

    # [Case 1] 마지막 씬: 길이를 건드리지 않고 인코딩만 수행 (Veo3 8초 그대로 사용)
    if keep_original_duration:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            video_out
        ])
        return actual  # 원본 길이 반환

    # [Case 2] 일반 씬: 시간 맞추기 로직
    tol = 0.03      # 30ms
    SMALL_PAD_SEC = 0.5 

    # 2-1. 거의 맞거나, 원본이 더 길면 -> Trim
    if abs(actual - target_sec) <= tol or actual > target_sec:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            video_out
        ])
        return target_sec

    # 2-2. 원본이 짧으면 -> Pad or Loop
    short_by = target_sec - actual

    if short_by <= SMALL_PAD_SEC:
        # 미세하게 짧으면 정지 화면(Pad)
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", f"{vf_base},tpad=stop_mode=clone:stop_duration={short_by:.3f}",
            "-t", f"{target_sec:.3f}",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",
            video_out
        ])
        return actual # 사실상 target_sec가 됨

    # 많이 짧으면 Loop
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

        # (선택) 하위호환: durations_ms로 들어오면 sec로 변환
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

            # 2) Download Videos & Process
            fixed_video_paths = []
            debug_scenes = []

            for i, (gs_url, dur) in enumerate(zip(videos, durations_sec)):
                # 다운로드
                raw_vp = os.path.join(tmpdir, f"video_raw_{i}.mp4")
                download_gs(gs_url, raw_vp)

                # 변환 경로
                fixed_vp = os.path.join(tmpdir, f"video_fixed_{i}.mp4")
                
                # 목표 시간 (ms -> sec)
                target_sec = float(dur)

                # [핵심 변경] 마지막 씬인지 확인
                is_last_scene = (i == len(videos) - 1)

                if is_last_scene:
                    # 마지막 씬은 자르지 않고 원본(8초) 그대로 사용
                    # target_sec는 무시됨
                    raw_sec = normalize_scene(raw_vp, fixed_vp, target_sec, fps, keep_original_duration=True)
                    note = "Last scene: Kept original duration"
                else:
                    # 나머지는 자막 시간에 맞춰 칼같이 자름
                    raw_sec = normalize_scene(raw_vp, fixed_vp, target_sec, fps, keep_original_duration=False)
                    note = "Normal scene: Trimmed to script"

                fixed_video_paths.append(fixed_vp)
                debug_scenes.append({
                    "idx": i,
                    "target_sec": round(target_sec, 3),
                    "note": note
                })

            # 3) Concat (이어붙이기)
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

            # 4) [삭제됨] normalize_total_to_audio
            # 이제 영상 전체 길이를 강제로 줄이거나 늘리지 않습니다.
            # 영상 길이 = (앞선 씬들의 합) + (마지막 씬 8초) 가 되어 오디오보다 자연스럽게 길어집니다.

            # 5) Muxing (오디오 합치기)
            # -shortest 옵션 없음: 비디오가 오디오보다 길면, 긴 비디오를 끝까지 다 보여줌
            final_video = os.path.join(tmpdir, "final.mp4")
            run_cmd([
                "ffmpeg", "-y",
                "-i", merged_video,  # 비디오 (오디오보다 김)
                "-i", audio_path,    # 오디오 (비디오보다 짧음)
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",      # 재인코딩 없이 병합
                "-c:a", "aac",
                "-b:a", "192k",
                final_video
            ])

            # 6) Upload
            upload_gs(final_video, output)

        return jsonify({
            "ok": True,
            "output": output,
            "videoCount": len(videos),
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
