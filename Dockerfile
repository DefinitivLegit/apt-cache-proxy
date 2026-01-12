# Use a lightweight Python base image
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Create directories for persistent data to ensure permissions are correct
RUN mkdir -p /app/storage /app/data

# Expose the default port defined in config.json
EXPOSE 8080

# Command to run the application
CMD ["python", "main.py"]