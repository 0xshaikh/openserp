"""
OpenSERP Website Backend

Serves the frontend and provides API endpoints for:
- Running site audits (uses OpenSERP internally)
- AI-powered SEO analysis (uses Apinex free models)
- Keyword suggestions with AI enhancement
"""

import os
import sys
import json
from flask import Flask, request, jsonify, render_template, send_from_directory

# Add parent directory to path so we can import openserp
# server.py is at website/backend/, openserp.py is at repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

app = Flask(__name__, 
            template_folder='../templates',
            static_folder='../static',
            static_url_path='/static')

# Apinex API configuration
APINEX_API_KEY = os.environ.get('APINEX_API_KEY', 'sk-apx0d67dc8efaf086abf1f4750edaf11f45311e5c146dd4ad1')
APINEX_BASE_URL = 'https://api.apinex.bond/v1'
APINEX_MODELS = {
    'analysis': 'free/gpt-6-luna',      # General reasoning for SEO analysis
    'keywords': 'free/glm-5.3',         # Fast for keyword suggestions
    'summary': 'free/deepseek-v4-pro-0813',  # Complex tasks for reports
    'quick': 'free/gemini-3.8-flash',   # Fast responses for quick tasks
}


@app.route('/')
def index():
    """Serve the main page"""
    return render_template('index.html')


@app.route('/api/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'service': 'OpenSERP Website',
        'models': list(APINEX_MODELS.values())
    })


@app.route('/api/audit', methods=['POST'])
def run_audit():
    """
    Run an OpenSERP audit on a URL.
    
    Request body: { "url": "https://example.com", "max_pages": 20 }
    """
    data = request.get_json()
    url = data.get('url', '').strip()
    max_pages = min(int(data.get('max_pages', 20)), 50)  # Cap at 50
    
    if not url:
        return jsonify({'error': 'URL is required'}), 400
    
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    
    try:
        # Import and run OpenSERP
        import openserp
        result = openserp.run_audit(url, max_pages=max_pages, link_probes=min(max_pages, 25))
        
        # Convert to dictionary
        result_dict = result.to_dict()
        
        return jsonify({
            'url': url,
            'score': result_dict.get('score', 0),
            'max_score': 100,
            'issues': result_dict.get('issues', []),
            'pages_crawled': result_dict.get('pages_crawled', 0),
            'duration_s': result_dict.get('duration_s', 0),
            'metadata': {
                'domain': result_dict.get('domain', ''),
                'https': result_dict.get('https', {}),
                'robots': result_dict.get('robots', {})
            }
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/analyze', methods=['POST'])
def ai_analyze():
    """
    AI-powered SEO analysis using Apinex models.
    
    Request body: { "url": "https://example.com", "audit_data": {...} }
    """
    data = request.get_json()
    url = data.get('url', '')
    audit_data = data.get('audit_data', {})
    
    # Build the analysis prompt
    score = audit_data.get('score', 'N/A')
    issues = audit_data.get('issues', [])
    issue_count = len(issues)
    
    prompt = f"""You are an expert SEO analyst. Analyze this website audit and provide actionable recommendations.

Website: {url}
Overall Score: {score}/100
Issues Found: {issue_count}

Issues:
{json.dumps(issues[:10], indent=2)}

Provide:
1. Top 3 priority fixes (ordered by impact)
2. Quick wins (things that take < 1 hour)
3. Long-term improvements
4. Estimated time to fix each issue

Be specific and actionable. Reference the actual issues found."""

    try:
        response = call_apinex(APINEX_MODELS['analysis'], prompt, max_tokens=1000)
        return jsonify({
            'analysis': response,
            'model': APINEX_MODELS['analysis'],
            'url': url
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/keywords', methods=['POST'])
def ai_keywords():
    """
    AI-powered keyword suggestions using Apinex models.
    
    Request body: { "seed": "seo audit", "count": 10 }
    """
    data = request.get_json()
    seed = data.get('seed', 'seo').strip()
    count = min(int(data.get('count', 10)), 50)
    
    prompt = f"""Generate {count} SEO keyword ideas related to "{seed}".

Format as JSON array:
[{{"keyword": "...", "intent": "informational|commercial|transactional|navigational", "difficulty": "low|medium|high", "opportunity": "high|medium|low"}}]

Focus on:
- Long-tail variations
- Question-based keywords
- Comparison keywords
- "How to" keywords
- Tool/software keywords

Return only valid JSON."""

    try:
        response = call_apinex(APINEX_MODELS['keywords'], prompt, max_tokens=2000)
        
        # Try to parse as JSON
        try:
            # Clean up markdown code blocks if present
            response = response.strip()
            if response.startswith('```'):
                response = response.split('```')[1]
                if response.startswith('json'):
                    response = response[4:]
            keywords = json.loads(response)
            return jsonify({
                'keywords': keywords,
                'model': APINEX_MODELS['keywords'],
                'seed': seed
            })
        except json.JSONDecodeError:
            # If JSON parsing fails, return as text
            return jsonify({
                'keywords': response,
                'model': APINEX_MODELS['keywords'],
                'seed': seed,
                'raw': True
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/ai/summary', methods=['POST'])
def ai_summary():
    """
    Generate an AI summary of audit results.
    
    Request body: { "url": "https://example.com", "audit_data": {...} }
    """
    data = request.get_json()
    url = data.get('url', '')
    audit_data = data.get('audit_data', {})
    
    score = audit_data.get('score', 'N/A')
    issues = audit_data.get('issues', [])
    
    issue_summary = []
    for issue in issues[:5]:
        issue_summary.append(f"- {issue.get('rule', 'Unknown')}: {issue.get('message', 'Issue found')}")
    
    prompt = f"""Write a concise SEO audit summary for {url}.

Score: {score}/100
Top Issues:
{chr(10).join(issue_summary) if issue_summary else 'No issues found!'}

Write a 3-4 sentence summary that:
1. States the overall score and grade
2. Highlights the most critical issues
3. Gives a quick recommendation
4. Is professional but easy to understand

Keep it under 100 words."""

    try:
        response = call_apinex(APINEX_MODELS['summary'], prompt, max_tokens=300)
        return jsonify({
            'summary': response,
            'model': APINEX_MODELS['summary'],
            'url': url
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def call_apinex(model, prompt, max_tokens=500, temperature=0.7):
    """Call Apinex API and return the response text."""
    import requests
    
    response = requests.post(
        f'{APINEX_BASE_URL}/chat/completions',
        headers={
            'Authorization': f'Bearer {APINEX_API_KEY}',
            'Content-Type': 'application/json'
        },
        json={
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': temperature
        },
        timeout=60
    )
    
    if not response.ok:
        error_data = response.json()
        raise Exception(error_data.get('error', {}).get('message', f'HTTP {response.status_code}'))
    
    data = response.json()
    return data.get('choices', [{}])[0].get('message', {}).get('content', '')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f'OpenSERP Website running on http://localhost:{port}')
    print(f'Apinex API Key: {APINEX_API_KEY[:10]}...')
    print(f'Models: {APINEX_MODELS}')
    app.run(host='0.0.0.0', port=port, debug=True)