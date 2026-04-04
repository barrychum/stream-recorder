FROM node:20-alpine

# Install ffmpeg
RUN apk add --no-cache ffmpeg tzdata

# Set timezone
#ENV TZ=Europe/London
ENV TZ=Hongkong

WORKDIR /app

# Copy backend
COPY backend/package*.json ./
RUN npm install --production

COPY backend/server.js ./

# Copy frontend
COPY frontend/ ./frontend/

# Create data directory for persistent schedule storage
RUN mkdir -p /data

# Default recordings directory
RUN mkdir -p /recordings

EXPOSE 3000

VOLUME ["/data", "/recordings"]

CMD ["node", "server.js"]
