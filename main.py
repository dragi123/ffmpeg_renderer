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

# 9:16 캔버스 강제
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


def normalize_scene(
    video_in: str,
    video_out: str,
    target_sec: float,
    fps: int,
    keep_original_duration: bool = False
) -> float:
    """
    씬 mp4 처리 함수
    - keep_original_duration=True (마지막 씬용): 길이 자르지 않고 포맷(9:16, fps)만 변환
    - False (일반 씬): target_sec에 맞춰서 Trim 또는 Loop/Pad
    """
    actual = ffprobe_duration_sec(video_in)

    # 공통 필터: 9:16 + fps 통일
    vf_base = f"{VF_916},fps={fps}"

    # [Case 1] 마지막 씬: 길이를 건드리지 않고 인코딩만 수행
    if keep_original_duration:
        run_cmd([
            "ffmpeg", "-y", "-i", video_in,
            "-vf", vf_base,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            "-an",
            video_out
        ])
        return actual  # 원본 길이 반환

    # [Case 2] 일반 씬: 시간 맞추기 로직
    tol = 0.03        # 30ms
    SMALL_PAD_SEC = 0.5

    # 2-1. 거의 맞거나, 원본이 더 길면 -> Trim
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
            "-r", str(fps),
            "-an",
            video_out
        ])
        return target_sec

    # 많이 짧으면 Loop
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
    audio_in에서 start_sec부터 dur_sec만큼 잘라서 AAC(m4a)로 저장.
    - pad_to_sec가 주어지면, 결과 오디오를 pad_to_sec 길이가 되도록 무음(apad)로 패딩.
      (마지막 씬 tail용)
    """
    if dur_sec <= 0:
        dur_sec = 0.001

    # mp3 -> m4a(AAC)
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

    # pad_to_sec까지 무음으로 패딩
    # -af apad=pad_dur=... 로 tail만큼 늘리고,
    # -t pad_to_sec 로 최종 길이를 고정
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


def mux_video_audio(video_in: str, audio_in: str, out_mp4: str, fps: int):
    """
    video_in + audio_in -> out_mp4
    - 영상은 copy(이미 libx264로 통일된 상태)
    - 오디오는 aac 유지
    """
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

            # 오디오 총 길이 (안전장치)
            audio_total_sec = ffprobe_duration_sec(audio_path)

            # 2) Process each scene:
            #    - video normalize (target or keep last original duration)
            #    - cut audio segment by cumulative timeline
            #    - mux into seg_i.mp4
            seg_paths = []
            debug_scenes = []

            cur_start = 0.0  # 누적 오디오 타임라인 시작점

            for i, (gs_url, dur) in enumerate(zip(videos, durations_sec)):
                target_sec = float(dur)
                is_last = (i == len(videos) - 1)

                # 다운로드
                raw_vp = os.path.join(tmpdir, f"video_raw_{i}.mp4")
                download_gs(gs_url, raw_vp)

                # 비디오 정규화
                fixed_vp = os.path.join(tmpdir, f"video_fixed_{i}.mp4")

                if is_last:
                    # 마지막 씬: 길이는 유지(예: Veo의 8초 그대로)
                    video_sec = normalize_scene(
                        raw_vp, fixed_vp, target_sec, fps, keep_original_duration=True
                    )
                else:
                    # 일반 씬: target_sec에 맞춤
                    video_sec = normalize_scene(
                        raw_vp, fixed_vp, target_sec, fps, keep_original_duration=False
                    )

                # 오디오 잘라오기 (누적 시작점 기준)
                # 남은 오디오보다 길면 캡 (안전)
                remaining = max(0.0, audio_total_sec - cur_start)
                audio_seg_sec = min(target_sec, remaining)

                audio_seg = os.path.join(tmpdir, f"audio_seg_{i}.m4a")

                if is_last:
                    # 마지막 씬: 오디오를 target만큼(또는 남은만큼) 가져오고
                    #          영상이 더 길면 무음으로 패딩해서 video_sec까지 채움
                    cut_audio_segment_to_aac(
                        audio_in=audio_path,
                        audio_out=audio_seg,
                        start_sec=cur_start,
                        dur_sec=audio_seg_sec,
                        pad_to_sec=video_sec  # <- 여기서 tail(무음) 처리
                    )
                else:
                    # 일반 씬: 그냥 target 길이만큼 (정확히 컷 전환 맞춤)
                    cut_audio_segment_to_aac(
                        audio_in=audio_path,
                        audio_out=audio_seg,
                        start_sec=cur_start,
                        dur_sec=audio_seg_sec,
                        pad_to_sec=None
                    )

                # 씬별 mux 결과
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
                    "note": "Last scene kept video duration; audio padded with silence" if is_last else "Scene muxed to script duration"
                })

                # 다음 씬 시작점 업데이트: "대본 기준" 누적으로 이동
                # (컷 전환 기준을 스크립트 타임라인으로 고정)
                cur_start += target_sec

            # 3) Concat segments (seg_i.mp4 이어붙이기)
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for vp in seg_paths:
                    f.write(f"file '{vp}'\n")

            final_video = os.path.join(tmpdir, "final.mp4")
            # seg들은 codec/param 통일 상태라 -c copy로 빠르게 concat 가능
            run_cmd([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c", "copy",
                final_video
            ])

            # 4) Upload
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
