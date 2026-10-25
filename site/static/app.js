/* ============================================
   OpenSERP Website — Interactive Demo
   ============================================ */

document.addEventListener('DOMContentLoaded', () => {
    // Smooth scroll for nav links
    document.querySelectorAll('a[href^="#"]').forEach(link => {
        link.addEventListener('click', (e) => {
            e.preventDefault();
            const target = document.querySelector(link.getAttribute('href'));
            if (target) {
                target.scrollIntoView({ behavior: 'smooth' });
            }
        });
    });

    // Demo functionality - AI Analysis
    const runAnalysisBtn = document.getElementById('run-analysis');
    const resultsContainer = document.getElementById('demo-results');
    
    if (runAnalysisBtn) {
        runAnalysisBtn.addEventListener('click', async () => {
            const url = document.getElementById('demo-url').value.trim();
            const apiKey = document.getElementById('api-key').value.trim();
            
            if (!url) {
                showEmptyState('Please enter a URL');
                return;
            }
            
            // Ensure URL has protocol
            let analysisUrl = url;
            if (!url.startsWith('http://') && !url.startsWith('https://')) {
                analysisUrl = 'https://' + url;
            }
            
            // Show loading state
            showLoading('Analyzing ' + analysisUrl + ' with AI...');
            
            try {
                // Call Apinex API for AI analysis
                const analysis = await callAI(analysisUrl, apiKey);
                showAIAnalysis(analysis);
            } catch (error) {
                showEmptyState('Error: ' + error.message + '. Check your API key or try again.');
            }
        });
    }
});

async function callAI(url, apiKey) {
    // Use Apinex free model
    const model = 'free/gpt-6-luna';
    const apiUrl = 'https://api.apinex.bond/v1/chat/completions';
    
    const prompt = `You are an expert SEO analyst. Analyze this website and provide actionable recommendations.

Website: ${url}

Provide:
1. Top 3 priority fixes (ordered by impact)
2. Quick wins (things that take < 1 hour)
3. Long-term improvements
4. Estimated time to fix each issue

Be specific and actionable. Use markdown formatting for readability.`;

    const headers = {
        'Content-Type': 'application/json'
    };
    
    if (apiKey) {
        headers['Authorization'] = 'Bearer ' + apiKey;
    }
    
    const response = await fetch(apiUrl, {
        method: 'POST',
        headers: headers,
        body: JSON.stringify({
            model: model,
            messages: [{ role: 'user', content: prompt }],
            max_tokens: 1000,
            temperature: 0.7
        })
    });
    
    if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error?.message || `HTTP ${response.status}`);
    }
    
    const data = await response.json();
    return data.choices?.[0]?.message?.content || 'No analysis received.';
}

function showEmptyState(message) {
    const container = document.getElementById('demo-results');
    container.innerHTML = `
        <div class="empty-state">
            <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="11" cy="11" r="8"></circle>
                <path d="m21 21-4.35-4.35"></path>
            </svg>
            <p>${message}</p>
        </div>
    `;
}

function showLoading(message) {
    const container = document.getElementById('demo-results');
    container.innerHTML = `
        <div class="loading-spinner">
            <div class="spinner"></div>
            <p>${message}</p>
        </div>
    `;
}

function showAIAnalysis(analysis) {
    const container = document.getElementById('demo-results');
    container.innerHTML = `
        <div class="ai-section">
            ${formatMarkdown(analysis)}
        </div>
    `;
    container.scrollTop = 0;
}

function formatMarkdown(text) {
    // Escape HTML
    text = text.replace(/&/g, '&amp;')
               .replace(/</g, '&lt;')
               .replace(/>/g, '&gt;');
    
    // Code blocks
    text = text.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    
    // Inline code
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    
    // Bold
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    
    // Italic
    text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    
    // Headings
    text = text.replace(/^### (.+)$/gm, '<h4>$1</h4>');
    text = text.replace(/^## (.+)$/gm, '<h3>$1</h3>');
    text = text.replace(/^# (.+)$/gm, '<h2>$1</h2>');
    
    // Links
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" style="color:#10b981; text-decoration:underline">$1</a>');
    
    // Lists
    text = text.replace(/^[\-\*] (.+)$/gm, '<li>$1</li>');
    text = text.replace(/(<li>.*<\/li>)/gs, '<ul>$1</ul>');
    
    // Line breaks
    text = text.replace(/\n\n/g, '</p><p>');
    text = text.replace(/\n/g, '<br>');
    
    return text;
}

// Store API key in localStorage
document.addEventListener('DOMContentLoaded', () => {
    const apiKeyInput = document.getElementById('api-key');
    const savedKey = localStorage.getItem('openserp_api_key');
    if (savedKey) {
        apiKeyInput.value = savedKey;
    }
    
    apiKeyInput.addEventListener('input', () => {
        localStorage.setItem('openserp_api_key', apiKeyInput.value);
    });
});