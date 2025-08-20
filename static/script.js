document.addEventListener('DOMContentLoaded', () => {
    // --- Element Variables ---
    const goalEditor = document.getElementById('goal-editor');
    const toolbox = document.getElementById('toolbox');
    const toolboxContent = document.getElementById('toolbox-content');
    const toggleToolboxBtn = document.getElementById('toggle-toolbox-btn');
    const startBtn = document.getElementById('start-btn');
    const stopBtn = document.getElementById('stop-btn');
    const pauseBtn = document.getElementById('pause-btn');
    const pauseInfo = document.getElementById('pause-info');
    const logOutput = document.getElementById('log-output');
    
    // --- State Variables ---
    let logInterval;

    // --- Functions ---
    function scrollToBottom() {
        logOutput.scrollTop = logOutput.scrollHeight;
    }

    async function fetchLog() {
        try {
            // Add a cache-busting query parameter
            const response = await fetch(`/log?t=${new Date().getTime()}`);
            const text = await response.text();
            logOutput.textContent = text;
            scrollToBottom();
        } catch (error) {
            console.error('Error fetching log:', error);
        }
    }

    async function loadTools() {
        try {
            const response = await fetch('/get_tools');
            const toolData = await response.json();
            toolboxContent.innerHTML = ''; // Clear

            for (const categoryKey in toolData) {
                const category = toolData[categoryKey];
                
                // Create Accordion Button
                const button = document.createElement('button');
                button.className = 'accordion-header';
                button.textContent = category.title;
                
                // Create Accordion Content Panel
                const panel = document.createElement('div');
                panel.className = 'accordion-content';
                
                category.tools.forEach(tool => {
                    const item = document.createElement('div');
                    item.className = 'draggable-item';
                    item.textContent = tool.name;
                    item.draggable = true;
                    item.dataset.tool = JSON.stringify(tool.data);
                    item.dataset.category = categoryKey;
                    panel.appendChild(item);
                });

                toolboxContent.appendChild(button);
                toolboxContent.appendChild(panel);
            }
            initializeAccordion();
            initializeDragAndDrop();
        } catch (error) {
            console.error('Error loading tools:', error);
            toolboxContent.textContent = 'Failed to load tools.';
        }
    }

    function initializeAccordion() {
        const headers = document.querySelectorAll('.accordion-header');
        headers.forEach(header => {
            header.addEventListener('click', () => {
                header.classList.toggle('active');
                const panel = header.nextElementSibling;
                if (panel.style.maxHeight) {
                    panel.style.maxHeight = null;
                } else {
                    panel.style.maxHeight = panel.scrollHeight + "px";
                }
            });
        });
    }

    function initializeDragAndDrop() {
        // Drag from Toolbox
        document.querySelectorAll('.draggable-item').forEach(item => {
            item.addEventListener('dragstart', e => {
                e.dataTransfer.setData('text/plain', JSON.stringify({
                    tool: item.dataset.tool,
                    category: item.dataset.category,
                    name: item.textContent
                }));
            });
        });
        
        // Drop into Editor
        goalEditor.addEventListener('dragover', e => e.preventDefault());
        goalEditor.addEventListener('drop', e => {
            e.preventDefault();
            const data = JSON.parse(e.dataTransfer.getData('text/plain'));
            insertPill(data.name, data.category, data.tool);
        });
    }

    function insertPill(name, category, toolData) {
        const pill = document.createElement('span');
        pill.className = 'tool-pill';
        pill.contentEditable = false;
        pill.dataset.category = category;
        pill.dataset.tool = toolData;
        
        const text = document.createTextNode(name);
        pill.appendChild(text);

        const removeBtn = document.createElement('span');
        removeBtn.className = 'remove-pill';
        removeBtn.textContent = ' \u00D7'; // Multiplication sign '×'
        removeBtn.onclick = () => pill.remove();
        pill.appendChild(removeBtn);

        // Insert at cursor
        const selection = window.getSelection();
        if (selection.rangeCount > 0) {
            const range = selection.getRangeAt(0);
            range.deleteContents();
            range.insertNode(pill);
            // Move cursor after the pill
            range.setStartAfter(pill);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
        } else {
            goalEditor.appendChild(pill);
        }
    }

    function goalToString() {
        let result = "";
        goalEditor.childNodes.forEach(node => {
            if (node.nodeType === Node.TEXT_NODE) {
                result += node.textContent;
            } else if (node.nodeType === Node.ELEMENT_NODE && node.classList.contains('tool-pill')) {
                const category = node.dataset.category;
                const toolData = node.dataset.tool;
                result += `${category}${toolData}`;
            } else if (node.nodeType === Node.ELEMENT_NODE) { // Handle divs/ps from browser
                result += node.textContent;
            }
        });
        return result.trim();
    }
    
    async function checkAgentStatus() {
        try {
            const response = await fetch('/status');
            const data = await response.json();
            if (data.is_running) {
                startBtn.disabled = true;
                stopBtn.disabled = false;
                pauseBtn.disabled = false;
                // Start polling logs if not already
                if (!logInterval) {
                    logInterval = setInterval(fetchLog, 1000);
                }
            } else {
                startBtn.disabled = false;
                stopBtn.disabled = true;
                pauseBtn.disabled = true;
                if (logInterval) clearInterval(logInterval);
            }
        } catch (error) {
            console.error('Error checking agent status:', error);
            startBtn.disabled = false;
            stopBtn.disabled = true;
            pauseBtn.disabled = true;
        }
    }

    // --- Event Listeners ---
    toggleToolboxBtn.addEventListener('click', () => {
        toolbox.classList.toggle('hidden');
    });

    startBtn.addEventListener('click', async () => {
        const goal = goalToString();
        if (!goal) {
            alert('Please enter a goal for the agent.');
            return;
        }

        try {
            const response = await fetch('/start', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ goal }),
            });

            const result = await response.json();
            if (result.status === 'success') {
                checkAgentStatus(); // Update UI based on new status
            } else {
                alert('Error starting agent: ' + result.message);
            }
        } catch (error) {
            console.error('Error starting agent:', error);
            alert('A network error occurred. Could not start the agent.');
        }
    });

    stopBtn.addEventListener('click', async () => {
        try {
            const response = await fetch('/stop', {
                method: 'POST',
            });
            const result = await response.json();
            if (result.status === 'success') {
                checkAgentStatus(); // Update UI based on new status
            } else {
                alert('Error stopping agent: ' + result.message);
            }
        } catch (error) {
            console.error('Error stopping agent:', error);
            alert('A network error occurred. Could not stop the agent.');
        }
    });

    pauseBtn.addEventListener('click', async () => {
        const isPaused = pauseBtn.classList.contains('paused');
        const shouldPause = !isPaused;

        // If we are resuming, first update the goal
        if (!shouldPause) {
            try {
                await fetch('/update_goal', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ goal: goalToString() }),
                });
            } catch (error) {
                console.error('Error updating goal:', error);
                alert('Could not update goal before resuming. Agent will continue with old goal.');
            }
        }

        try {
            await fetch('/pause', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ pause: shouldPause }),
            });
            
            if (shouldPause) {
                pauseBtn.textContent = 'Resume with New Goal';
                pauseBtn.classList.add('paused');
                pauseInfo.classList.remove('hidden');
            } else {
                pauseBtn.textContent = 'Pause';
                pauseBtn.classList.remove('paused');
                pauseInfo.classList.add('hidden');
            }
        } catch (error) {
            console.error('Error toggling pause:', error);
        }
    });

    // Initial check on page load
    checkAgentStatus();
    loadTools();
});
