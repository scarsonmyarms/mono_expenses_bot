# Mono Expenses Telegram Bot

## Description
A Telegram bot built to automate personal financial tracking. It allows users to log cash transactions directly through a chat interface, immediately syncing the data to a Google Spreadsheet. The project is designed for quick access and reliable expense management.

## Features
- **Real-time Logging:** Instantly record daily expenses via Telegram commands or messages.
- **Google Sheets Integration:** Connects securely to Google Sheets using Google Cloud Platform (GCP) service accounts to store and organize financial data.
- **Webhook Deployment:** Configured with webhooks for instantaneous updates and hosted on Render.
- **Security-Minded:** Designed with secure credential management in mind, complying with GitHub push protection standards for GCP keys.

## Tech Stack
- Python (Aiogram, Flask)
- Google Cloud Platform (GCP) & Google Sheets API
- Webhooks
- Render (Deployment)
