# Use Python base image (not slim - Playwright needs more dependencies)
FROM python:3.14

# Set working directory
WORKDIR /app

# Prevent Python from writing pyc files and enforce unbuffered stdout for container logs
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers and system dependencies
RUN playwright install chromium && playwright install-deps chromium

# Copy the application code
COPY main.py geocoding.py team_colours.py .

# Run the script
CMD ["python", "main.py"]
