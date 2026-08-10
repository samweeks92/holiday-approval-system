FROM python:3.12-slim

WORKDIR /code

RUN pip install --no-cache-dir \
    fastapi uvicorn google-adk google-cloud-aiplatform sse-starlette pydantic requests google-auth

COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
