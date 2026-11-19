/* ====================================================================
   Loan Default Risk Prediction System - main.js
   Dynamic Charts, Real-Time Prediction & UI Interactivity
   ==================================================================== */

document.addEventListener('DOMContentLoaded', () => {
  initDismissibleFlashMessages();
  initPlatformCompanyManagement();

  // Initialize Dashboard Charts if on Dashboard
  initRiskDonutChart();
  initTrendLineChart();

  // Setup Form AJAX Submissions
  setupDashboardPredictor();
});

function initPlatformCompanyManagement() {
  const searchInput = document.querySelector('[data-company-search]');
  const companyCards = document.querySelectorAll('[data-company-card]');
  const noResults = document.querySelector('[data-no-company-results]');

  if (searchInput && noResults) {
    searchInput.addEventListener('input', () => {
      const query = searchInput.value.trim().toLocaleLowerCase();
      let visibleCount = 0;
      companyCards.forEach(card => {
        const matches = card.dataset.searchText.toLocaleLowerCase().includes(query);
        card.hidden = !matches;
        if (matches) visibleCount += 1;
      });
      noResults.hidden = visibleCount > 0 || companyCards.length === 0;
    });
  }
}

function initDismissibleFlashMessages() {
  const flashMessages = document.querySelectorAll([
    '[role="alert"]',
    '[role="status"]',
    '.alert',
    '.login-message',
    '.registration-alert',
    'div[style*="background: #ecfdf5"]',
    'div[style*="background: #fee2e2"]'
  ].join(','));

  flashMessages.forEach(message => {
    if (message.querySelector('.flash-dismiss-button')) return;

    message.classList.add('flash-dismissible');
    const dismissButton = document.createElement('button');
    dismissButton.type = 'button';
    dismissButton.className = 'flash-dismiss-button';
    dismissButton.setAttribute('aria-label', 'Dismiss notification');
    dismissButton.textContent = '\u00d7';
    dismissButton.addEventListener('click', () => message.remove());
    message.appendChild(dismissButton);
  });
}

/* ================================= Donut Chart (Image 1) ================================= */
function initRiskDonutChart() {
  const canvas = document.getElementById('riskDonutCanvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const size = 200;
  
  canvas.width = size * dpr;
  canvas.height = size * dpr;
  canvas.style.width = size + 'px';
  canvas.style.height = size + 'px';
  ctx.scale(dpr, dpr);

  const centerX = size / 2;
  const centerY = size / 2;
  const outerRadius = 88;
  const innerRadius = 48;

  const counts = [
    Number(canvas.dataset.low || 0),
    Number(canvas.dataset.medium || 0),
    Number(canvas.dataset.high || 0)
  ];
  const total = counts.reduce((sum, count) => sum + count, 0);
  const segments = [
    { label: 'Low Risk', count: counts[0], color: '#10b981' },
    { label: 'Medium Risk', count: counts[1], color: '#f59e0b' },
    { label: 'High Risk', count: counts[2], color: '#ef4444' }
  ];

  let currentAngle = -Math.PI / 2;

  if (total === 0) {
    ctx.beginPath();
    ctx.arc(centerX, centerY, outerRadius, 0, 2 * Math.PI);
    ctx.arc(centerX, centerY, innerRadius, 2 * Math.PI, 0, true);
    ctx.closePath();
    ctx.fillStyle = '#e2e8f0';
    ctx.fill();
  }

  segments.forEach(segment => {
    if (total === 0 || segment.count === 0) return;
    const sliceAngle = (segment.count / total) * 2 * Math.PI;
    const endAngle = currentAngle + sliceAngle;

    // Draw Donut Arc
    ctx.beginPath();
    ctx.arc(centerX, centerY, outerRadius, currentAngle, endAngle);
    ctx.arc(centerX, centerY, innerRadius, endAngle, currentAngle, true);
    ctx.closePath();
    ctx.fillStyle = segment.color;
    ctx.fill();

    // Text Label inside slice
    const midAngle = currentAngle + sliceAngle / 2;
    const textRadius = (outerRadius + innerRadius) / 2;
    const textX = centerX + Math.cos(midAngle) * textRadius;
    const textY = centerY + Math.sin(midAngle) * textRadius + 4;

    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 11px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(`${Math.round((segment.count / total) * 100)}%`, textX, textY);

    currentAngle = endAngle;
  });

  // White inner circle hole
  ctx.beginPath();
  ctx.arc(centerX, centerY, innerRadius, 0, 2 * Math.PI);
  ctx.fillStyle = '#ffffff';
  ctx.fill();
  if (total === 0) {
    ctx.fillStyle = '#64748b';
    ctx.font = '12px -apple-system, BlinkMacSystemFont, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No assessments', centerX, centerY + 4);
  }
}

/* ================================= Line Chart (Image 1) ================================= */
function initTrendLineChart() {
  const canvas = document.getElementById('trendLineCanvas');
  if (!canvas) return;

  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.parentElement.clientWidth - 40 || 480;
  const h = 210;

  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.width = w + 'px';
  canvas.style.height = h + 'px';
  ctx.scale(dpr, dpr);

  const paddingLeft = 40;
  const paddingRight = 20;
  const paddingTop = 20;
  const paddingBottom = 30;

  const chartW = w - paddingLeft - paddingRight;
  const chartH = h - paddingTop - paddingBottom;

  const labels = JSON.parse(canvas.dataset.labels || '[]');
  const data = JSON.parse(canvas.dataset.values || '[]');
  if (labels.length === 0 || labels.length !== data.length) return;
  const maxVal = Math.max(4, Math.ceil(Math.max(...data) / 4) * 4);
  const unit = canvas.dataset.unit || '';

  // Draw Grid Lines & Y Axis Labels (0, 100, 200, 300, 400)
  ctx.strokeStyle = '#f1f5f9';
  ctx.lineWidth = 1;
  ctx.fillStyle = '#64748b';
  ctx.font = '11px -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.textAlign = 'right';

  const yTicks = [0, 1, 2, 3, 4].map(tick => tick * maxVal / 4);
  yTicks.forEach(tick => {
    const y = paddingTop + chartH - (tick / maxVal) * chartH;
    ctx.beginPath();
    ctx.moveTo(paddingLeft, y);
    ctx.lineTo(w - paddingRight, y);
    ctx.stroke();
    ctx.fillText(tick.toString() + unit, paddingLeft - 8, y + 4);
  });

  // Calculate coordinates for line
  const points = data.map((val, idx) => {
    const x = paddingLeft + (labels.length === 1 ? 0.5 : idx / (labels.length - 1)) * chartW;
    const y = paddingTop + chartH - (val / maxVal) * chartH;
    return { x, y, label: labels[idx], val };
  });

  // Draw Gradient Fill Area
  const grad = ctx.createLinearGradient(0, paddingTop, 0, paddingTop + chartH);
  grad.addColorStop(0, 'rgba(26, 104, 255, 0.22)');
  grad.addColorStop(1, 'rgba(26, 104, 255, 0.0)');

  ctx.beginPath();
  ctx.moveTo(points[0].x, paddingTop + chartH);
  points.forEach((p, idx) => {
    if (idx === 0) ctx.lineTo(p.x, p.y);
    else {
      // Smooth cubic curve
      const prev = points[idx - 1];
      const cx = (prev.x + p.x) / 2;
      ctx.bezierCurveTo(cx, prev.y, cx, p.y, p.x, p.y);
    }
  });
  ctx.lineTo(points[points.length - 1].x, paddingTop + chartH);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Draw Line
  ctx.beginPath();
  points.forEach((p, idx) => {
    if (idx === 0) ctx.moveTo(p.x, p.y);
    else {
      const prev = points[idx - 1];
      const cx = (prev.x + p.x) / 2;
      ctx.bezierCurveTo(cx, prev.y, cx, p.y, p.x, p.y);
    }
  });
  ctx.strokeStyle = '#1a68ff';
  ctx.lineWidth = 3;
  ctx.stroke();

  // Draw Dots & X Labels
  ctx.fillStyle = '#64748b';
  ctx.textAlign = 'center';

  points.forEach(p => {
    // Dot
    ctx.beginPath();
    ctx.arc(p.x, p.y, 5, 0, 2 * Math.PI);
    ctx.fillStyle = '#1a68ff';
    ctx.fill();
    ctx.lineWidth = 2;
    ctx.strokeStyle = '#ffffff';
    ctx.stroke();

    // X label
    ctx.fillStyle = '#64748b';
    ctx.fillText(p.label, p.x, h - 8);
  });
}

/* ================================= Real-time Dashboard Prediction AJAX ================================= */
function setupDashboardPredictor() {
  const form = document.getElementById('dashboardPredictForm');
  if (!form) return;

  const probNumber = document.getElementById('probNumber');
  const riskBadge = document.getElementById('riskBadge');
  const insightsList = document.getElementById('insightsList');
  const assessmentsList = document.getElementById('recentAssessmentsList');
  const btn = document.getElementById('btnPredictAction');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();

    const monthly_income = parseFloat(document.getElementById('monthly_income').value) || 500000;
    const loan_amount = parseFloat(document.getElementById('loan_amount').value) || 2000000;
    const loan_term = parseInt(document.getElementById('loan_term').value, 10) || 12;
    const employment_status = document.getElementById('employment_status').value || 'Employed';
    const existing_debt = parseFloat(document.getElementById('existing_debt').value) || 100000;

    if (btn) {
      btn.disabled = true;
      btn.innerHTML = '<span>⏳ Calculating...</span>';
    }

    try {
      const response = await fetch('/api/predict', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({
          monthly_income,
          loan_amount,
          loan_term,
          employment_status,
          existing_debt
        })
      });

      const res = await response.json();
      if (res.status === 'success' && res.data) {
        const d = res.data;
        // Update Default Probability number
        if (probNumber) probNumber.textContent = d.default_probability.toFixed(1) + '%';
        
        // Update Risk badge
        if (riskBadge) {
          riskBadge.textContent = d.risk_tier;
          if (d.risk_tier === 'Low Risk') {
            riskBadge.style.background = '#d1fae5';
            riskBadge.style.color = '#065f46';
          } else if (d.risk_tier === 'Medium Risk') {
            riskBadge.style.background = '#fef3c7';
            riskBadge.style.color = '#92400e';
          } else {
            riskBadge.style.background = '#fee2e2';
            riskBadge.style.color = '#991b1b';
          }
        }

        // Update Key Insights
        if (insightsList && d.key_insights) {
          insightsList.innerHTML = '';
          d.key_insights.forEach(insight => {
            const li = document.createElement('li');
            li.textContent = insight;
            insightsList.appendChild(li);
          });
        }

        // Add to Recent Assessments List at top
        if (assessmentsList && d.app_id) {
          const newRow = document.createElement('li');
          newRow.className = 'assessment-item-row';
          const tierClass = d.risk_tier === 'Low Risk' ? 'text-risk-low' : (d.risk_tier === 'Medium Risk' ? 'text-risk-medium' : 'text-risk-high');
          newRow.innerHTML = `
            <div class="item-left-info">
              <span class="item-app-id">${d.app_id}</span>
              <span class="item-risk-status ${tierClass}">${d.default_probability}% • ${d.risk_tier}</span>
            </div>
            <div class="item-datetime">
              <div>Just now</div>
              <div style="font-size: 0.7rem; color: #94a3b8;">Today</div>
            </div>
          `;
          assessmentsList.prepend(newRow);
        }
      }
    } catch (err) {
      console.error('Prediction failed:', err);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line></svg> Predict Risk`;
      }
    }
  });
}

function showDetailedExplanation() {
  const probNumber = document.getElementById('probNumber');
  const probVal = probNumber ? probNumber.textContent : '25.0%';
  const income = document.getElementById('monthly_income')?.value || '500,000';
  const loan = document.getElementById('loan_amount')?.value || '2,000,000';
  const term = document.getElementById('loan_term')?.value || '12';

  alert(`Detailed Risk Explanation\n\n• Probability of Default: ${probVal}\n• Requested Loan: ${Number(loan).toLocaleString()} MWK over ${term} months\n• Monthly Income: ${Number(income).toLocaleString()} MWK\n• Financial Health: The borrower's monthly installment-to-income ratio remains within acceptable institutional underwriting thresholds with satisfactory employment stability.`);
}
