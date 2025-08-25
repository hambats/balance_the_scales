# Balance the Scales

A lightweight household task-balancing web app built with Node.js, Express, TailwindCSS, and Docker.

## Overview

**Balance the Scales** helps households fairly track and distribute recurring chores. Each member logs completed tasks, and the app visualizes progress using weighted categories.

## Features

- **Households**
  - The first user creates a household and gets a share code.
  - Others can join using that share code.

- **Users**
  - Multiple users per household.
  - Update usernames in Settings.

- **Categories**
  - Define chore categories (e.g. Dishes, Laundry).
  - Assign weights so tougher chores count more.

- **Task Logging**
  - One-click task logging.
  - Visual progress bars for each user’s share.

- **History**
  - Activity log displays recent tasks by user.

- **Settings**
  - Change username
  - Copy household share code
  - Toggle dark mode
  - Log out (clears cookies)

- **Responsive UI**
  - Built with TailwindCSS.
  - Works seamlessly on desktop and mobile.
  - Bottom navigation highlights the current section.

## Quick Start (Docker)

# 1. Clone the repo
git clone https://github.com/hambats/balance_the_scales.git
cd balance_the_scales

# 2. Build the Docker image
docker build -t balance_the_scales .

# 3. Run the container (requires a 32-byte hex encryption key)
docker run -p 3000:3000 \
  -e ENCRYPTION_KEY=$(openssl rand -hex 32) \
  -v "$(pwd)/data:/data" \
  balance_the_scales

Then open your browser to http://localhost:3000.
Encryption at Rest

Household data is encrypted using AES-256-GCM. Supply a 32-byte hex ENCRYPTION_KEY when running Docker. The encrypted database is stored at /data/data.enc; use a mounted volume to preserve data.
Development Setup

For live reloading during development:

docker run -p 3000:3000 \
  -v "$(pwd)":/usr/src/app \
  balance_the_scales

Edit files locally—changes apply instantly in the container.
Hosting Online

Easily expose your instance to the web using reverse proxies like Nginx Proxy Manager, Caddy, or Cloudflare Tunnel.
API Endpoints
Endpoint	Method	Input	Output
/api/create-household	POST	{ "name": "UserName" }	{ household_id, user_id, share_code }
/api/join-household	POST	{ "code": "ABC123", "name": "UserName" }	{ household_id, user_id }
/api/users	GET	?household_id=1	{ users: [...], share_code: "ABC123" }
/api/categories	GET	?household_id=1	Categories list with weights and task counts
	POST	{ "household_id":1, "name":"Laundry", "weight":1 }	new category
/api/task	POST	{ "user_id": 1, "category_id": 1 }	Log a completed task
/api/history	GET	?household_id=1	Task history logs
/api/update-user	POST	{ "user_id": 1, "name": "NewName" }	Update username

Example (history output):

[
  {
    "user": "Alice",
    "category": "Dishes",
    "time": "2025-08-24T12:34:56Z"
  }
]

Project Structure

balance_the_scales/
├── Dockerfile           # Container build instructions
├── server.js            # Node.js backend
├── package.json         # Dependencies and scripts
├── index.html           # Frontend UI
├── data.enc             # Encrypted household data (ignored in git)
└── README.md            # This file

Tech Stack

    Backend: Node.js, Express

    Frontend: TailwindCSS, Font Awesome

    Deployment: Docker

Contributing

Pull requests are welcome! For significant changes, please open an issue first to discuss.
License

Licensed under GPL-3.0. Feel free to review the full license in LICENSE.
