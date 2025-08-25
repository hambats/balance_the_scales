# Balance the Scales

A lightweight household task balancing web app.
Built with Node.js, Express, and TailwindCSS, and packaged for easy deployment with Docker.

## Overview

Balance the Scales helps households fairly distribute and track recurring tasks.
Each member logs the chores they complete, and the app tracks progress using task categories and configurable weights.

## Features

Households

* First user creates a household which generates a share code
* Other users join using that share code

Users

* Each household can have multiple users
* Usernames can be updated in the Settings tab

Categories

* Add tasks grouped into categories such as Dishes or Laundry
* Categories can have weights so harder chores count more

Task Logging

* One click logging of completed tasks
* Visual progress bars show each user’s share

History

* Recent activity log of completed tasks by user

Settings

* Update username
* Copy household code
* Toggle dark mode
* Log out which clears cookies

UI

* Built with TailwindCSS
* Works on desktop and mobile
* Bottom navigation highlights the active section

## Quick Start with Docker

Make sure Docker is installed.

1. Clone the repository

```
git clone https://github.com/yourusername/balance-scales.git
cd balance-scales
```

2. Build the image

```
docker build -t balance-scales .
```

3. Run the container (generate a 32-byte hex encryption key first)

```
docker run -p 3000:3000 \
  -e ENCRYPTION_KEY=$(openssl rand -hex 32) \
  -v $(pwd)/data:/data \
  balance-scales
```

Open [http://localhost:3000](http://localhost:3000)

### Encryption

Household data is encrypted at rest using AES-256-GCM. Supply a 32-byte hex
`ENCRYPTION_KEY` when running the container. The encrypted database is stored in
`/data/data.enc` by default, so mount a volume to persist it.

## Development Setup

If you want live reloading instead of rebuilding images

```
docker run -p 3000:3000 \
  -v $(pwd):/usr/src/app \
  balance-scales
```

Then edit files locally. Changes will apply inside the container immediately.

## Hosting Online

This service can be exposed on the web via a reverse proxy.
Common setups include Nginx Proxy Manager, Caddy, or Cloudflare Tunnel.

## API Endpoints

The backend provides a simple JSON API. All requests and responses use JSON.

* POST `/api/create-household`
  Input: `{ "name": "UserName" }`
  Output: `{ "household_id": 1, "user_id": 1, "share_code": "ABC123" }`

* POST `/api/join-household`
  Input: `{ "code": "ABC123", "name": "UserName" }`
  Output: `{ "household_id": 1, "user_id": 2 }`

* GET `/api/users?household_id=1`
  Output: `{ "users": [...], "share_code": "ABC123" }`

* GET `/api/categories?household_id=1`
  Output: `[ { "id": 1, "name": "Dishes", "weight": 2, "task_counts": { "1": 3 } } ]`

* POST `/api/categories`
  Input: `{ "household_id": 1, "name": "Laundry", "weight": 1 }`

* POST `/api/task`
  Input: `{ "user_id": 1, "category_id": 1 }`

* GET `/api/history?household_id=1`
  Output: `[ { "user": "Alice", "category": "Dishes", "time": "2025-08-24T12:34:56Z" } ]`

* POST `/api/update-user`
  Input: `{ "user_id": 1, "name": "NewName" }`

## Project Structure

```
balance-scales/
│── Dockerfile          # Container definition
│── server.js           # Node.js backend
│── package.json        # Dependencies
│── index.html          # Frontend UI
│── data.enc (ignored)  # Encrypted household data store
```

## Tech Stack

* Node.js and Express for the backend
* TailwindCSS for frontend styling
* Font Awesome for icons
* Docker for deployment

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

MIT License
