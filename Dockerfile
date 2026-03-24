FROM --platform=linux/amd64 ghcr.io/astral-sh/uv:python3.11-bookworm

WORKDIR /app
# Install dependencies from requirements.txt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .
# Expose the port that the application will run on
EXPOSE 9000

# Start the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]