import os
import json
import subprocess
import tempfile
from flask import Flask, request, jsonify
from google.cloud import storage

app = Flask(__name__)

# ---------- 기본 엔드포인트 ----------
@app.get("/")
def root():
    return "ffmpeg-renderer up", 200


@app.get("/health")
def health():
    return "ok", 200


# ---------- 유틸: n8n payload 파싱 ----------
def parse_payload(req):
    """
    n8n에서 올 수 있는 케이스들:
    1) Content-Type: application/json, body가 dict
    2) body 자체가 stringified JSON (data가 str)
    3) {"body": "{...json...}"} 형태로 한번 더 감싸서 오는 경우
    """
    data = req.get_json(force=True, silent=True)

    # 1) data가 문자열이면 JSON으로 한 번 파싱
    if isinstance(data, str):
        data = json.loads(data)

    # 2) {"body": "..."} 형태면 body를 다시 파싱
    if isinstance(data, dict) and "body" in data and isinstance(data["body"], str):
        try:
            inner = json.loads(data["body"])
            if isinstance(inner, dict):
                data = inner
        except Exception:
            pass

    return data


# ---------- 핵심 렌더 엔드포인트 ----------
@app.post("/render")
def render():
    try:
        # 디버그(로그에서 확인 가능)
        print(">>> /render called")
        print("content-type:", request.content_type)
        raw_preview = request.get_data(as_text=True)[:500]
        print("raw preview:", raw_preview)

        # 1) 요청 JSON 받기
        data = parse_payload(request)

        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "Invalid JSON payload"}), 400

        audio = data.get("audio")
        videos = data.get("videos")
        output = data.get("output")

        if not audio or not videos or not output:
            return jsonify({
                "ok": False,
                "error": "payload must include audio, videos[], output",
                "receivedKeys": list(data.keys())
            }), 400

        if not isinstance(videos, list) or len(videos) == 0:
            return jsonify({"ok": False, "error": "videos must be a non-empty array"}), 400

        # 2) GCS 클라이언트
        client = storage.Client()

        def download_gs(gs_path, local_path):
            if not gs_path.startswith("gs://"):
                raise ValueError(f"Invalid GCS path: {gs_path}")
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.download_to_filename(local_path)

        def upload_gs(local_path, gs_path):
            if not gs_path.startswith("gs://"):
                raise ValueError(f"Invalid GCS path: {gs_path}")
            bucket_name, blob_name = gs_path.replace("gs://", "").split("/", 1)
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            blob.upload_from_filename(local_path, content_type="video/mp4")

        # 3) 임시 디렉토리에서 작업
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "audio.mp3")
            download_gs(audio, audio_path)

            video_paths = []
            for i, v in enumerate(videos):
                vp = os.path.join(tmpdir, f"video_{i}.mp4")
                download_gs(v, vp)
                video_paths.append(vp)

            # concat 리스트 파일 생성
            concat_list = os.path.join(tmpdir, "concat.txt")
            with open(concat_list, "w", encoding="utf-8") as f:
                for vp in video_paths:
                    # ffmpeg concat demuxer 포맷: file '/path/to/file'
                    f.write(f"file '{vp}'\n")

            merged_video = os.path.join(tmpdir, "merged.mp4")
            final_video = os.path.join(tmpdir, "final.mp4")

            # 4) 영상 concat (재인코딩: 입력 영상들이 codec/timebase가 달라도 안전하게)
            cmd_concat = [
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", concat_list,
                "-c:v", "libx264",
                "-preset", "fast",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                merged_video
            ]
            print("FFMPEG CONCAT:", " ".join(cmd_concat))
            subprocess.run(cmd_concat, check=True, capture_output=True, text=True)

            # 5) 오디오 합성 (영상 copy + 오디오 aac)
            cmd_audio = [
                "ffmpeg", "-y",
                "-i", merged_video,
                "-i", audio_path,
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                "-movflags", "+faststart",
                final_video
            ]
            print("FFMPEG AUDIO:", " ".join(cmd_audio))
            subprocess.run(cmd_audio, check=True, capture_output=True, text=True)

            # 6) GCS 업로드
            upload_gs(final_video, output)

        return jsonify({
            "ok": True,
            "output": output,
            "videoCount": len(videos)
        }), 200

    except subprocess.CalledProcessError as e:
        # run(check=True, capture_output=True)라 stderr/stdout을 보여줄 수 있음
        detail = {
            "returncode": e.returncode,
            "cmd": str(e.cmd),
            "stdout": getattr(e, "stdout", None),
            "stderr": getattr(e, "stderr", None),
        }
        return jsonify({"ok": False, "error": "ffmpeg failed", "detail": detail}), 500

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": "internal error", "detail": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
