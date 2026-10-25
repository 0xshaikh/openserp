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

    // Demo audit functionality
    const runAuditBtn = document.getElementById('run-audit');
    const resultsContainer = document.getElementById('demo-results');
    
    if (runAuditBtn) {
        runAuditBtn.addEventListener('click', async () => {
            const url = document.getElementById('audit-url').value.trim();
            const maxPages = parseInt(document.getElementById('max-pages').value) || 15;
            
            if (!url) {
                showEmptyState('Please enter a URL');
                return;
            }
            
            // Show loading state
            showLoading('Running audit on ' + url + '...');
            
            try {
                // Run audit
                const response = await fetch('/api/audit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, max_pages: maxPages })
                });
                
                const data = await response.json();
                
                if (!response.ok || data.error) {
                    showEmptyState('Error: ' + (data.error || 'Audit failed'));
                    return;
                }
                
                // Show results
                showResults(data);
                
                // Get AI analysis
                const aiResponse = await fetch('/api/ai/analyze', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, audit_data: data })
                });
                
                const aiData = await aiResponse.json();
                
                if (aiData.analysis) {
                    showAIAnalysis(aiData.analysis);
                }
                
            } catch (error) {
                showEmptyState('Connection error: ' + error.message);
            }
        });
    }
});

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

function showResults(data) {
    const container = document.getElementById('demo-results');
    const score = data.score || 0;
    const issues = data.issues || [];
    
    // Determine score color
    let scoreColor = '#ef4444'; // red
    let grade = 'F';
    if (score >= 90) { scoreColor = '#10b981'; grade = 'A'; }
    else if (score >= 80) { scoreColor = '#84cc16'; grade = 'B'; }
    else if (score >= 70) { scoreColor = '#f59e0b'; grade = 'C'; }
    else if (score >= 60) { scoreColor = '#f97316'; grade = 'D'; }
    
    // Format issues
    const issuesHtml = issues.map(issue => {
        const severity = issue.severity || 'error';
        const severityClass = severity === 'warning' ? 'warning' : severity === 'info' ? 'info' : severity === 'pass' ? 'pass' : '';
        const rule = issue.rule || 'UNKNOWN';
        const message = issue.message || 'Issue found';
        
        return `
            <div class="issue-item ${severityClass}">
                <span class="issue-severity">${severity.toUpperCase()}</span>
                <div class="issue-content">
                    <div class="issue-title">${rule}</div>
                    <div class="issue-message">${message}</div>
                </div>
            </div>
        `;
    }).join('');
    
    container.innerHTML = `
        <div class="result-header">
            <div class="result-score">
                <span class="score-number" style="color: ${scoreColor}">${score}</span>
                <span class="score-label">/ 100 (${grade})</span>
            </div>
            <span class="result-url">${data.url}</span>
        </div>
        <div class="issues-list">
            ${issuesHtml || '<p style="color: var(--color-text-muted); text-align: center; padding: 20px;">No issues found!</p>'}
        </div>
    `;
}

function showAIAnalysis(analysis) {
    const container = document.getElementById('demo-results');
    const existingContent = container.innerHTML;
    
    container.innerHTML = existingContent + `
        <div class="ai-section">
            <div class="ai-header">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M12 2a10 10 0 1 0 10 10 10 10 0 0 0-10-10z"/>
                    <path d="M12 6v6l4 2"/>
                </svg>
                AI Analysis (Powered by Apinex)
            </div>
            <div class="ai-content">${formatAIResponse(analysis)}</div>
        </div>
    `;
    
    // Scroll to AI section
    container.scrollTop = container.scrollHeight;
}

function formatAIResponse(text) {
    // Format markdown-style text
    text = text.replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>');
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');
    return text;
}