const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');
const crypto = require('crypto');

const DATA_FILE = process.env.DATA_FILE || path.join(__dirname, 'data.enc');
const ENCRYPTION_KEY_HEX = process.env.ENCRYPTION_KEY || '';
if (ENCRYPTION_KEY_HEX.length !== 64) {
  console.error('ENCRYPTION_KEY must be a 32-byte hex string');
  process.exit(1);
}
const ENCRYPTION_KEY = Buffer.from(ENCRYPTION_KEY_HEX, 'hex');

function encrypt(plaintext) {
  const iv = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv('aes-256-gcm', ENCRYPTION_KEY, iv);
  const encrypted = Buffer.concat([cipher.update(plaintext, 'utf8'), cipher.final()]);
  const tag = cipher.getAuthTag();
  return JSON.stringify({
    iv: iv.toString('hex'),
    tag: tag.toString('hex'),
    data: encrypted.toString('hex')
  });
}

function decrypt(encText) {
  const { iv, tag, data } = JSON.parse(encText);
  const decipher = crypto.createDecipheriv('aes-256-gcm', ENCRYPTION_KEY, Buffer.from(iv, 'hex'));
  decipher.setAuthTag(Buffer.from(tag, 'hex'));
  const decrypted = Buffer.concat([
    decipher.update(Buffer.from(data, 'hex')),
    decipher.final()
  ]);
  return decrypted.toString('utf8');
}

// Load data from disk or return a default structure
function loadData() {
  try {
    const enc = fs.readFileSync(DATA_FILE, 'utf8');
    const json = decrypt(enc);
    return JSON.parse(json);
  } catch (err) {
    return {
      households: [],
      next_household_id: 1,
      next_user_id: 1,
      next_category_id: 1,
      next_task_id: 1
    };
  }
}

// Save data back to disk
function saveData(data) {
  const enc = encrypt(JSON.stringify(data));
  fs.writeFileSync(DATA_FILE, enc, 'utf8');
}

// Generate a share code using a restricted character set to avoid ambiguity
function generateShareCode(length = 6) {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let code = '';
  for (let i = 0; i < length; i++) {
    code += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return code;
}

// Serve static files from the project root
function serveStatic(req, res) {
  let filePath = req.url.split('?')[0];
  if (filePath === '/' || filePath === '') {
    filePath = '/index.html';
  }
  const ext = path.extname(filePath).toLowerCase();
  const mimeTypes = {
    '.html': 'text/html',
    '.js': 'application/javascript',
    '.css': 'text/css',
    '.json': 'application/json',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml'
  };
  const absPath = path.join(__dirname, filePath);
  fs.readFile(absPath, (err, content) => {
    if (err) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('Not Found');
    } else {
      const type = mimeTypes[ext] || 'application/octet-stream';
      res.writeHead(200, { 'Content-Type': type });
      res.end(content);
    }
  });
}

// Handle all API endpoints under /api/
function handleApi(req, res) {
  const urlObj = new URL(req.url, `http://${req.headers.host}`);
  const pathName = urlObj.pathname;
  const method = req.method;
  const data = loadData();

  // Helper for sending JSON responses
  function sendJSON(status, obj) {
    res.writeHead(status, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(obj));
  }

  // Users endpoint
  if (method === 'GET' && pathName === '/api/users') {
    const householdId = parseInt(urlObj.searchParams.get('household_id') || '');
    const household = data.households.find(h => h.id === householdId);
    if (!household) return sendJSON(404, { error: 'Household not found' });
    return sendJSON(200, { users: household.users, share_code: household.share_code });
  }

  // Update user name
  if (method === 'POST' && pathName === '/api/update-user') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { user_id, name } = JSON.parse(body);
        if (!user_id || !name) return sendJSON(400, { error: 'user_id and name required' });
        for (const h of data.households) {
          const user = h.users.find(u => u.id === user_id);
          if (user) {
            user.name = name;
            saveData(data);
            return sendJSON(200, { success: true, user });
          }
        }
        return sendJSON(404, { error: 'User not found' });
      } catch {
        return sendJSON(400, { error: 'Invalid JSON' });
      }
    });
    return;
  }

  // Create household
  if (method === 'POST' && pathName === '/api/create-household') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { name } = JSON.parse(body);
        if (!name) return sendJSON(400, { error: 'name required' });
        const householdId = data.next_household_id++;
        const userId = data.next_user_id++;
        const shareCode = generateShareCode();
        const household = {
          id: householdId,
          share_code: shareCode,
          users: [{ id: userId, name }],
          categories: [],
          tasks: [],
          overall_counts: { [userId]: 0 }
        };
        data.households.push(household);
        saveData(data);
        return sendJSON(200, { household_id: householdId, user_id: userId, share_code: shareCode });
      } catch {
        return sendJSON(400, { error: 'Invalid JSON' });
      }
    });
    return;
  }

  // Join household
  if (method === 'POST' && pathName === '/api/join-household') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { code, name } = JSON.parse(body);
        if (!code || !name) return sendJSON(400, { error: 'code and name required' });
        const household = data.households.find(h => h.share_code.toUpperCase() === code.toUpperCase());
        if (!household) return sendJSON(404, { error: 'Invalid code' });
        const userId = data.next_user_id++;
        household.users.push({ id: userId, name });
        household.overall_counts = household.overall_counts || {};
        household.overall_counts[userId] = 0;
        for (const cat of household.categories) {
          cat.task_counts = cat.task_counts || {};
          cat.task_counts[userId] = 0;
        }
        saveData(data);
        return sendJSON(200, { household_id: household.id, user_id: userId });
      } catch {
        return sendJSON(400, { error: 'Invalid JSON' });
      }
    });
    return;
  }

  // Get categories
  if (method === 'GET' && pathName === '/api/categories') {
    const householdId = parseInt(urlObj.searchParams.get('household_id') || '');
    const household = data.households.find(h => h.id === householdId);
    if (!household) return sendJSON(404, { error: 'Household not found' });
    const categories = household.categories.map(cat => {
      cat.task_counts = cat.task_counts || {};
      const taskCounts = { ...cat.task_counts };
      for (const u of household.users) {
        if (taskCounts[u.id] === undefined) {
          taskCounts[u.id] = 0;
        }
      }
      return { id: cat.id, name: cat.name, weight: cat.weight || 1, task_counts: taskCounts };
    });
    return sendJSON(200, categories);
  }

  // Master scale
  if (method === 'GET' && pathName === '/api/master-scale') {
    const householdId = parseInt(urlObj.searchParams.get('household_id') || '');
    const household = data.households.find(h => h.id === householdId);
    if (!household) return sendJSON(404, { error: 'Household not found' });
    household.overall_counts = household.overall_counts || {};
    const counts = { ...household.overall_counts };
    for (const u of household.users) {
      if (counts[u.id] === undefined) {
        counts[u.id] = 0;
      }
    }
    return sendJSON(200, { overall_counts: counts });
  }

  // Create category
  if (method === 'POST' && pathName === '/api/categories') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { household_id, name, weight } = JSON.parse(body);
        const householdId = parseInt(household_id || '');
        if (!householdId || !name) return sendJSON(400, { error: 'household_id and name required' });
        const household = data.households.find(h => h.id === householdId);
        if (!household) return sendJSON(404, { error: 'Household not found' });
        if (household.categories.some(c => c.name.toLowerCase() === name.toLowerCase())) {
          return sendJSON(409, { error: 'Category already exists' });
        }
        const categoryId = data.next_category_id++;
        const taskCounts = {};
        for (const u of household.users) {
          taskCounts[u.id] = 0;
        }
        household.categories.push({ id: categoryId, name, weight: weight || 1, task_counts: taskCounts });
        saveData(data);
        return sendJSON(200, { id: categoryId, name, weight: weight || 1 });
      } catch {
        return sendJSON(400, { error: 'Invalid JSON' });
      }
    });
    return;
  }

  // Log task
  if (method === 'POST' && pathName === '/api/task') {
    let body = '';
    req.on('data', chunk => body += chunk);
    req.on('end', () => {
      try {
        const { user_id, category_id } = JSON.parse(body);
        const userId = parseInt(user_id || '');
        const categoryId = parseInt(category_id || '');
        if (!userId || !categoryId) return sendJSON(400, { error: 'user_id and category_id required' });
        let household = null;
        let category = null;
        outer: for (const h of data.households) {
          const userExists = h.users.some(u => u.id === userId);
          const cat = h.categories.find(c => c.id === categoryId);
          if (userExists && cat) {
            household = h;
            category = cat;
            break outer;
          }
        }
        if (!household || !category) return sendJSON(404, { error: 'Invalid user or category' });
        category.task_counts = category.task_counts || {};
        category.task_counts[userId] = (category.task_counts[userId] || 0) + 1;
        household.overall_counts = household.overall_counts || {};
        household.overall_counts[userId] = (household.overall_counts[userId] || 0) + (category.weight || 1);
        const taskId = data.next_task_id++;
        household.tasks.push({ id: taskId, user_id: userId, category_id: categoryId, timestamp: new Date().toISOString() });
        saveData(data);
        return sendJSON(200, { success: true, task_id: taskId });
      } catch {
        return sendJSON(400, { error: 'Invalid JSON' });
      }
    });
    return;
  }

  // Get history
  if (method === 'GET' && pathName === '/api/history') {
    const householdId = parseInt(urlObj.searchParams.get('household_id') || '');
    const household = data.households.find(h => h.id === householdId);
    if (!household) return sendJSON(404, { error: 'Household not found' });
    const history = household.tasks
      .slice(-20)
      .reverse()
      .map(t => {
        const user = household.users.find(u => u.id === t.user_id);
        const cat = household.categories.find(c => c.id === t.category_id);
        return {
          user: user ? user.name : '',
          category: cat ? cat.name : '',
          time: new Date(t.timestamp).toLocaleString()
        };
      });
    return sendJSON(200, history);
  }

  // If no endpoint matched
  return sendJSON(404, { error: 'Not found' });
}

// Create and start the HTTP server
const server = http.createServer((req, res) => {
  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    return res.end();
  }
  if (req.url.startsWith('/api/')) {
    return handleApi(req, res);
  }
  return serveStatic(req, res);
});

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});