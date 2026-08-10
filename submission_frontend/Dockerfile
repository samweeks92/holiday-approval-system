# Build Stage
FROM node:20-slim AS build

WORKDIR /app/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ .
RUN npm run build

# Production Stage
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend code
COPY api/ .

# Copy built frontend assets
COPY --from=build /app/ui/dist ./static

# Expose port
EXPOSE 8080

# Environment variables (Defaults, override in Cloud Run)
ENV PORT=8080
ENV HOST=0.0.0.0

# Run application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
