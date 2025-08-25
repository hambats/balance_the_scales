FROM node:18-alpine
WORKDIR /usr/src/app
# Copy application source into container
COPY . .
# Expose the application port
EXPOSE 3000
CMD ["node", "server.js"]