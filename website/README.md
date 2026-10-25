# OpenSERP Website

Professional website for OpenSERP with live demo and AI-powered analysis.

## Features

- **Live Demo** — Run real site audits directly in the browser
- **AI-Powered Analysis** — SEO recommendations powered by Apinex free models
- **Responsive Design** — Works on desktop, tablet, and mobile
- **Professional UI/UX** — Clean, modern design with smooth animations

## Tech Stack

- **Frontend**: Vanilla HTML, CSS, JavaScript (no frameworks)
- **Backend**: Python Flask
- **AI Models**: Apinex free models (gpt-6-luna, glm-5.3, deepseek-v4-pro)
- **Hosting**: Self-hosted or any Python hosting platform

## Setup

```bash
# Install dependencies
pip install flask

# Run the server
cd website/backend
python3 server.py

# Open http://localhost:5000
```

## API Endpoints

- `GET /` — Main website
- `GET /api/health` — Health check
- `POST /api/audit` — Run site audit
- `POST /api/ai/analyze` — AI-powered SEO analysis
- `POST /api/ai/keywords` — AI keyword suggestions
- `POST /api/ai/summary` — AI audit summary

## AI Models Used

| Task | Model | Purpose |
|------|-------|---------|
| SEO Analysis | free/gpt-6-luna | General reasoning |
| Keywords | free/glm-5.3 | Fast quality |
| Summary | free/deepseek-v4-pro-0813 | Complex tasks |
| Quick | free/gemini-3.8-flash | Fast responses |

## Screenshots

- [Hero Section](screenshots/openserp-hero.png)
- [Features](screenshots/openserp-features.png)
- [Demo](screenshots/openserp-demo.png)
- [Results](screenshots/openserp-results.png)

## Environment Variables

```bash
APINEX_API_KEY=your_key_here  # Get from apinex.bond
```

## License

MIT License. Part of the OpenSERP project.