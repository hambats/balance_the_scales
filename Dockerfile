FROM node:18-alpine
WORKDIR /usr/src/app
COPY package.json package-lock.json* ./
RUN npm install --production
COPY . .
VOLUME ["/data"]
ENV DATA_FILE=/data/data.enc
EXPOSE 3000
CMD ["node", "server.js"]