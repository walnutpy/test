FROM python:3.10-slim

# 작업 디렉터리
WORKDIR /app

RUN python -m pip install --upgrade pip setuptools==78.1.1 wheel
# 의존성 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt


# 앱 소스 복사
COPY . .

# Flask가 바인딩할 포트
EXPOSE 5000

# 컨테이너 시작 시 실행 명령
CMD ["python", "app.py"]