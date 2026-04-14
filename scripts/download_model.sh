#!/bin/bash
# Downloads the MediaPipe FaceLandmarker model required for head pose compensation.
# Safe to run multiple times — skips download if file already exists.

MODEL_FILE="face_landmarker.task"
MODEL_URL="https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"

if [ -f "$MODEL_FILE" ]; then
    echo "[model] $MODEL_FILE already exists — skipping download."
    exit 0
fi

echo "[model] Downloading FaceLandmarker model (~30MB) ..."
wget -q --show-progress -O "$MODEL_FILE" "$MODEL_URL"

if [ $? -eq 0 ]; then
    echo "[model] Download complete: $MODEL_FILE"
else
    echo "[model] ERROR: Download failed. Check your internet connection."
    exit 1
fi
