# Use the official Playwright Python image with all dependencies pre-installed
FROM mcr.microsoft.com/playwright:python-v1.40.0

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . .

# Install Chromium (already in base image, but we ensure it's there)
RUN playwright install chromium

# Set environment variables (can be overridden at runtime)
ENV PYTHONUNBUFFERED=1
ENV HEADLESS=True

# Run the bot
CMD ["python", "main.py"]
