@echo off
docker build --platform linux/amd64 -t satamanchuk/alexbot .
if %errorlevel% neq 0 exit /b %errorlevel%
docker push satamanchuk/alexbot
