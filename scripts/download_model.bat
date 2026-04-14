@echo off
REM Downloads the MediaPipe FaceLandmarker model (~30MB)
SET MODEL_FILE=face_landmarker.task
SET MODEL_URL=https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task

IF EXIST "%MODEL_FILE%" (
    echo [model] %MODEL_FILE% already exists -- skipping download.
    goto :done
)

echo [model] Downloading FaceLandmarker model (~30MB) ...
curl -L -o "%MODEL_FILE%" "%MODEL_URL%"

IF %ERRORLEVEL% EQU 0 (
    echo [model] Download complete: %MODEL_FILE%
) ELSE (
    echo [model] ERROR: Download failed. Check your internet connection.
    echo [model] Or download manually from:
    echo %MODEL_URL%
)

:done
pause
