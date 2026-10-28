FROM nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04
RUN apt-get update && apt-get install -y python3-pip && pip install faster-whisper fastapi uvicorn python-multipart
COPY contrib/faster_whisper_server.py /app/server.py
CMD ["python3", "/app/server.py"]
