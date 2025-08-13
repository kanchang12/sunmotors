/**
 * Waste King Thomas Chatbot Widget
 * Production version for any website
 * 
 * Usage: Add this script tag before closing </body> tag:
 * <script src="https://your-domain.com/waste-king-chatbot.js"></script>
 */

(function() {
    'use strict';

    // Prevent multiple initializations
    if (window.WasteKingChatbotLoaded) return;
    window.WasteKingChatbotLoaded = true;

    // Default configuration - can be overridden by window.WasteKingConfig
    const DEFAULT_CONFIG = {
        apiBaseUrl: 'https://internal-porpoise-onewebonly-1b44fcb9.koyeb.app',
        position: 'bottom-right',
        primaryColor: '#28a745',
        secondaryColor: '#20c997',
        fontFamily: "'Segoe UI', Tahoma, Geneva, Verdana, sans-serif",
        phoneNumber: '0800 123 4567',
        welcomeMessage: "Good day! This is Thomas from Waste King. How may I help you today?",
        welcomeDelay: 2000
    };

    // Merge user config with defaults
    const CONFIG = Object.assign({}, DEFAULT_CONFIG, window.WasteKingConfig || {});

    // CSS Styles
    const CSS = `
        .wk-chatbot-container {
            position: fixed;
            ${CONFIG.position.includes('bottom') ? 'bottom: 20px;' : 'top: 20px;'}
            ${CONFIG.position.includes('right') ? 'right: 20px;' : 'left: 20px;'}
            z-index: 999999;
            font-family: ${CONFIG.fontFamily};
        }
        .wk-chat-toggle {
            width: 60px;
            height: 60px;
            border-radius: 50%;
            background: linear-gradient(45deg, ${CONFIG.primaryColor}, ${CONFIG.secondaryColor});
            border: none;
            cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3);
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s ease;
            position: relative;
        }
        .wk-chat-toggle:hover {
            transform: scale(1.1);
            box-shadow: 0 6px 20px rgba(0,0,0,0.4);
        }
        .wk-chat-toggle svg {
            width: 28px;
            height: 28px;
            fill: white;
        }
        .wk-notification-badge {
            position: absolute;
            top: -5px;
            right: -5px;
            background: #dc3545;
            color: white;
            border-radius: 50%;
            width: 20px;
            height: 20px;
            font-size: 12px;
            display: flex;
            align-items: center;
            justify-content: center;
            animation: wk-pulse 2s infinite;
        }
        @keyframes wk-pulse {
            0% { transform: scale(1); }
            50% { transform: scale(1.1); }
            100% { transform: scale(1); }
        }
        .wk-chat-window {
            position: absolute;
            ${CONFIG.position.includes('bottom') ? 'bottom: 80px;' : 'top: 80px;'}
            ${CONFIG.position.includes('right') ? 'right: 0;' : 'left: 0;'}
            width: 350px;
            height: 500px;
            background: white;
            border-radius: 15px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
            display: none;
            flex-direction: column;
            overflow: hidden;
            animation: wk-slideUp 0.3s ease;
        }
        @keyframes wk-slideUp {
            from { transform: translateY(20px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        .wk-chat-header {
            background: linear-gradient(45deg, ${CONFIG.primaryColor}, ${CONFIG.secondaryColor});
            color: white;
            padding: 15px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        .wk-chat-header h3 {
            margin: 0;
            font-size: 16px;
            font-weight: 600;
        }
        .wk-chat-status {
            font-size: 12px;
            opacity: 0.9;
        }
        .wk-chat-close {
            background: none;
            border: none;
            color: white;
            font-size: 20px;
            cursor: pointer;
            padding: 0;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .wk-chat-messages {
            flex: 1;
            overflow-y: auto;
            padding: 15px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            background: #f8f9fa;
        }
        .wk-message {
            max-width: 85%;
            padding: 10px 14px;
            border-radius: 18px;
            font-size: 14px;
            line-height: 1.4;
            animation: wk-messageSlide 0.3s ease;
        }
        @keyframes wk-messageSlide {
            from { transform: translateY(10px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        .wk-message.bot {
            align-self: flex-start;
            background: white;
            border: 1px solid #e9ecef;
            color: #333;
        }
        .wk-message.user {
            align-self: flex-end;
            background: ${CONFIG.primaryColor};
            color: white;
        }
        .wk-message.system {
            align-self: center;
            background: #ffc107;
            color: #333;
            font-size: 12px;
            padding: 6px 12px;
        }
        .wk-typing-indicator {
            display: none;
            align-self: flex-start;
            background: white;
            border: 1px solid #e9ecef;
            padding: 10px 14px;
            border-radius: 18px;
            font-size: 14px;
            color: #666;
        }
        .wk-typing-dots {
            display: inline-flex;
            gap: 3px;
        }
        .wk-typing-dots span {
            width: 6px;
            height: 6px;
            background: #666;
            border-radius: 50%;
            animation: wk-typing 1.4s infinite;
        }
        .wk-typing-dots span:nth-child(2) { animation-delay: 0.2s; }
        .wk-typing-dots span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes wk-typing {
            0%, 60%, 100% { opacity: 0.3; }
            30% { opacity: 1; }
        }
        .wk-chat-input-area {
            padding: 15px;
            background: white;
            border-top: 1px solid #e9ecef;
        }
        .wk-chat-input-container {
            display: flex;
            gap: 10px;
            align-items: flex-end;
        }
        .wk-chat-input {
            flex: 1;
            border: 1px solid #ddd;
            border-radius: 20px;
            padding: 10px 15px;
            font-size: 14px;
            resize: none;
            min-height: 20px;
            max-height: 80px;
            font-family: inherit;
        }
        .wk-chat-input:focus {
            outline: none;
            border-color: ${CONFIG.primaryColor};
        }
        .wk-chat-send {
            background: ${CONFIG.primaryColor};
            border: none;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            display: flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .wk-chat-send:hover {
            background: #218838;
        }
        .wk-chat-send:disabled {
            background: #ccc;
            cursor: not-allowed;
        }
        .wk-chat-send svg {
            width: 18px;
            height: 18px;
            fill: white;
        }
        .wk-quick-actions {
            display: flex;
            gap: 8px;
            margin-top: 10px;
            flex-wrap: wrap;
        }
        .wk-quick-action {
            background: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 15px;
            padding: 6px 12px;
            font-size: 12px;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        .wk-quick-action:hover {
            background: #e9ecef;
        }
        @media (max-width: 480px) {
            .wk-chat-window {
                width: calc(100vw - 40px);
                height: calc(100vh - 120px);
                ${CONFIG.position.includes('bottom') ? 'bottom: 80px;' : 'top: 80px;'}
                ${CONFIG.position.includes('right') ? 'right: 20px;' : 'left: 20px;'}
            }
        }
        .wk-chat-messages::-webkit-scrollbar {
            width: 4px;
        }
        .wk-chat-messages::-webkit-scrollbar-track {
            background: #f1f1f1;
        }
        .wk-chat-messages::-webkit-scrollbar-thumb {
            background: #c1c1c1;
            border-radius: 2px;
        }
    `;

    // HTML Template
    const HTML_TEMPLATE = `
        <div class="wk-chatbot-container" id="wk-chatbot">
            <button class="wk-chat-toggle" id="wk-chat-toggle">
                <svg viewBox="0 0 24 24">
                    <path d="M20,2H4A2,2 0 0,0 2,4V22L6,18H20A2,2 0 0,0 22,16V4A2,2 0 0,0 20,2M6,9H18V11H6V9M14,14H6V12H14V14Z"/>
                </svg>
                <div class="wk-notification-badge" id="wk-notification" style="display: none;">1</div>
            </button>
            <div class="wk-chat-window" id="wk-chat-window">
                <div class="wk-chat-header">
                    <div>
                        <h3>Thomas - Waste King</h3>
                        <div class="wk-chat-status">Online • Typically replies instantly</div>
                    </div>
                    <button class="wk-chat-close" id="wk-chat-close">&times;</button>
                </div>
                <div class="wk-chat-messages" id="wk-chat-messages">
                    <div class="wk-typing-indicator" id="wk-typing-indicator">
                        <div class="wk-typing-dots">
                            <span></span>
                            <span></span>
                            <span></span>
                        </div>
                        Thomas is typing...
                    </div>
                </div>
                <div class="wk-chat-input-area">
                    <div class="wk-chat-input-container">
                        <textarea 
                            class="wk-chat-input" 
                            id="wk-chat-input" 
                            placeholder="Type your message..."
                            rows="1"
                        ></textarea>
                        <button class="wk-chat-send" id="wk-chat-send">
                            <svg viewBox="0 0 24 24">
                                <path d="M2,21L23,12L2,3V10L17,12L2,14V21Z"/>
                            </svg>
                        </button>
                    </div>
                    <div class="wk-quick-actions" id="wk-quick-actions">
                        <button class="wk-quick-action" data-message="I need a skip">Skip hire</button>
                        <button class="wk-quick-action" data-message="I need man and van">Man & Van</button>
                        <button class="wk-quick-action" data-message="I need grab hire">Grab hire</button>
                        <button class="wk-quick-action" data-message="Get a quote">Get quote</button>
                    </div>
                </div>
            </div>
        </div>
    `;

    // Main Chatbot Class
    class WasteKingChatbot {
        constructor() {
            this.apiBaseUrl = CONFIG.apiBaseUrl;
            this.conversationId = this.generateConversationId();
            this.chatState = {
                step: 'greeting',
                serviceType: null,
                wasteType: null,
                heavyMaterials: null,
                quantity: null,
                postcode: null,
                customerName: null,
                customerPhone: null,
                quoteId: null,
                isBooking: false
            };
            this.isProcessing = false;
        }

        generateConversationId() {
            return 'chat_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
        }

        init() {
            this.injectStyles();
            this.injectHTML();
            this.setupEventListeners();
            setTimeout(() => this.showWelcomeMessage(), CONFIG.welcomeDelay);
        }

        injectStyles() {
            const style = document.createElement('style');
            style.textContent = CSS;
            document.head.appendChild(style);
        }

        injectHTML() {
            const container = document.createElement('div');
            container.innerHTML = HTML_TEMPLATE;
            document.body.appendChild(container.firstElementChild);
        }

        setupEventListeners() {
            const toggleBtn = document.getElementById('wk-chat-toggle');
            const closeBtn = document.getElementById('wk-chat-close');
            const sendBtn = document.getElementById('wk-chat-send');
            const input = document.getElementById('wk-chat-input');
            const quickActions = document.getElementById('wk-quick-actions');

            toggleBtn.addEventListener('click', () => this.toggleChat());
            closeBtn.addEventListener('click', () => this.closeChat());
            sendBtn.addEventListener('click', () => this.sendMessage());
            
            input.addEventListener('keypress', (e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    this.sendMessage();
                }
            });

            input.addEventListener('input', () => this.autoResize());

            quickActions.addEventListener('click', (e) => {
                if (e.target.classList.contains('wk-quick-action')) {
                    const message = e.target.getAttribute('data-message');
                    this.sendQuickMessage(message);
                }
            });
        }

        toggleChat() {
            const chatWindow = document.getElementById('wk-chat-window');
            const notification = document.getElementById('wk-notification');
            
            if (chatWindow.style.display === 'none' || !chatWindow.style.display) {
                chatWindow.style.display = 'flex';
                notification.style.display = 'none';
                this.scrollToBottom();
                document.getElementById('wk-chat-input').focus();
            } else {
                chatWindow.style.display = 'none';
            }
        }

        closeChat() {
            document.getElementById('wk-chat-window').style.display = 'none';
        }

        showNotification() {
            const notification = document.getElementById('wk-notification');
            if (notification) notification.style.display = 'flex';
        }

        autoResize() {
            const input = document.getElementById('wk-chat-input');
            if (input) {
                input.style.height = 'auto';
                input.style.height = Math.min(input.scrollHeight, 80) + 'px';
            }
        }

        async showWelcomeMessage() {
            await this.delay(1000);
            this.addBotMessage(CONFIG.welcomeMessage);
            this.showNotification();
        }

        addMessage(message, type = 'user') {
            const messagesContainer = document.getElementById('wk-chat-messages');
            if (!messagesContainer) return;

            const messageDiv = document.createElement('div');
            messageDiv.className = `wk-message ${type}`;
            messageDiv.textContent = message;
            
            messagesContainer.appendChild(messageDiv);
            this.scrollToBottom();
            
            return messageDiv;
        }

        addBotMessage(message) {
            return this.addMessage(message, 'bot');
        }

        addSystemMessage(message) {
            return this.addMessage(message, 'system');
        }

        showTyping() {
            const typing = document.getElementById('wk-typing-indicator');
            if (typing) {
                typing.style.display = 'block';
                this.scrollToBottom();
            }
        }

        hideTyping() {
            const typing = document.getElementById('wk-typing-indicator');
            if (typing) typing.style.display = 'none';
        }

        scrollToBottom() {
            const messagesContainer = document.getElementById('wk-chat-messages');
            if (messagesContainer) {
                messagesContainer.scrollTop = messagesContainer.scrollHeight;
            }
        }

        delay(ms) {
            return new Promise(resolve => setTimeout(resolve, ms));
        }

        async sendMessage() {
            if (this.isProcessing) return;

            const input = document.getElementById('wk-chat-input');
            if (!input) return;

            const message = input.value.trim();
            if (!message) return;

            this.addMessage(message, 'user');
            input.value = '';
            this.autoResize();

            await this.processUserMessage(message);
        }

        async sendQuickMessage(message) {
            if (this.isProcessing) return;
            
            this.addMessage(message, 'user');
            await this.processUserMessage(message);
        }

        async processUserMessage(message) {
            this.isProcessing = true;
            this.showTyping();

            try {
                if (this.checkTriggerPoints(message)) {
                    return;
                }
                await this.handleChatFlow(message);
            } catch (error) {
                console.error('Error processing message:', error);
                this.addBotMessage(`I'm sorry, I'm experiencing technical difficulties. Please try again or contact us directly at ${CONFIG.phoneNumber}.`);
            } finally {
                this.hideTyping();
                this.isProcessing = false;
            }
        }

        checkTriggerPoints(message) {
            const lowerMessage = message.toLowerCase();

            if (lowerMessage.includes('pay a bill') || lowerMessage.includes('account') || 
                lowerMessage.includes('invoice') || lowerMessage.includes('outstanding balance')) {
                this.handleEscalation('payment', `I understand you need help with billing. Please contact our accounts team at ${CONFIG.phoneNumber}.`);
                return true;
            }

            if (lowerMessage.includes('complaint') || lowerMessage.includes('not happy') || 
                lowerMessage.includes('dissatisfied') || lowerMessage.includes('manager')) {
                this.handleEscalation('complaint', `I understand your frustration. Please contact our customer service manager at ${CONFIG.phoneNumber}.`);
                return true;
            }

            if (lowerMessage.includes('asbestos') || lowerMessage.includes('hazardous') || 
                lowerMessage.includes('chemical') || lowerMessage.includes('gas cylinder')) {
                this.handleEscalation('specialist', `We can help with that. Please contact our specialist team at ${CONFIG.phoneNumber}.`);
                return true;
            }

            if (lowerMessage.includes('transfer') || lowerMessage.includes('human') || 
                lowerMessage.includes('person') || lowerMessage.includes('agent')) {
                this.handleEscalation('human', `I'll connect you with our team. Please call ${CONFIG.phoneNumber}.`);
                return true;
            }

            return false;
        }

        async handleEscalation(type, message) {
            this.addBotMessage(message);
            await this.delay(1000);
            this.addSystemMessage(`For immediate assistance: ${CONFIG.phoneNumber}`);
        }

        async handleChatFlow(message) {
            switch (this.chatState.step) {
                case 'greeting':
                    await this.handleGreeting(message);
                    break;
                case 'service_type':
                    await this.handleServiceType(message);
                    break;
                case 'waste_type':
                    await this.handleWasteType(message);
                    break;
                case 'quantity':
                    await this.handleQuantity(message);
                    break;
                case 'postcode':
                    await this.handlePostcode(message);
                    break;
                case 'name':
                    await this.handleName(message);
                    break;
                case 'booking_intent':
                    await this.handleBookingIntent(message);
                    break;
                case 'phone_confirmation':
                    await this.handlePhoneConfirmation(message);
                    break;
                default:
                    await this.handleGeneralMessage(message);
            }
        }

        async handleGreeting(message) {
            const lowerMessage = message.toLowerCase();
            
            if (lowerMessage.includes('skip')) {
                this.chatState.serviceType = 'skip hire';
                await this.delay(800);
                this.addBotMessage("Great! I can help you with skip hire.");
                this.chatState.step = 'name';
                await this.delay(500);
                this.addBotMessage("Can I take your name please?");
            } else if (lowerMessage.includes('man') && lowerMessage.includes('van')) {
                this.chatState.serviceType = 'Man & Van';
                await this.delay(800);
                this.addBotMessage("Perfect! I can help you with our Man & Van service.");
                this.chatState.step = 'name';
                await this.delay(500);
                this.addBotMessage("Can I take your name please?");
            } else if (lowerMessage.includes('grab')) {
                this.chatState.serviceType = 'grab hire';
                await this.delay(800);
                this.addBotMessage("Excellent! I can help you with grab hire.");
                this.chatState.step = 'name';
                await this.delay(500);
                this.addBotMessage("Can I take your name please?");
            } else {
                await this.delay(800);
                this.addBotMessage("I can help you with waste removal! Is this for skip hire, Man & Van, or grab hire?");
                this.chatState.step = 'service_type';
            }
        }

        async handleServiceType(message) {
            const lowerMessage = message.toLowerCase();
            
            if (lowerMessage.includes('skip')) {
                this.chatState.serviceType = 'skip hire';
            } else if (lowerMessage.includes('man') || lowerMessage.includes('van')) {
                this.chatState.serviceType = 'Man & Van';
            } else if (lowerMessage.includes('grab')) {
                this.chatState.serviceType = 'grab hire';
            } else {
                this.addBotMessage("I didn't catch that. Could you please choose: skip hire, Man & Van, or grab hire?");
                return;
            }

            await this.delay(500);
            this.addBotMessage(`Great choice! Let's get your ${this.chatState.serviceType} sorted.`);
            this.chatState.step = 'name';
            await this.delay(500);
            this.addBotMessage("Can I take your name please?");
        }

        async handleName(message) {
            this.chatState.customerName = message;
            await this.delay(500);
            this.addBotMessage(`Nice to meet you, ${message}!`);
            this.chatState.step = 'waste_type';
            await this.delay(500);
            
            if (this.chatState.serviceType === 'skip hire') {
                this.addBotMessage("What type of waste will you be putting in the skip?");
            } else if (this.chatState.serviceType === 'Man & Van') {
                this.addBotMessage("What type of waste do you have?");
            } else {
                this.addBotMessage("What type of material will we be collecting?");
            }
        }

        async handleWasteType(message) {
            this.chatState.wasteType = message;
            const lowerMessage = message.toLowerCase();
            
            if (lowerMessage.includes('soil') || lowerMessage.includes('rubble') || 
                lowerMessage.includes('brick') || lowerMessage.includes('concrete') || 
                lowerMessage.includes('tile')) {
                this.chatState.heavyMaterials = true;
                await this.delay(500);
                this.addBotMessage("I can see you have heavy materials there.");
            } else {
                this.chatState.heavyMaterials = false;
            }
            
            this.chatState.step = 'quantity';
            await this.delay(500);
            this.addBotMessage("Is it a small job or a big job?");
        }

        async handleQuantity(message) {
            this.chatState.quantity = message;
            await this.delay(500);
            this.addBotMessage("Perfect!");
            this.chatState.step = 'postcode';
            await this.delay(500);
            this.addBotMessage("What's the postcode for delivery/collection?");
        }

        async handlePostcode(message) {
            const postcode = message.trim().toUpperCase();
            
            const postcodeRegex = /^[A-Z]{1,2}[0-9][A-Z0-9]?\s?[0-9][A-Z]{2}$/;
            if (!postcodeRegex.test(postcode)) {
                this.addBotMessage("That doesn't look like a valid UK postcode. Could you please check and try again? For example: M1 1AA or LS1 4ED");
                return;
            }

            this.chatState.postcode = postcode;
            await this.delay(500);
            this.addBotMessage(`Thanks, can you confirm ${postcode} is correct?`);
            
            await this.delay(2000);
            this.addBotMessage("There will be a moment of silence while I get you the price...");
            
            await this.getQuote();
        }

        async getQuote() {
            try {
                const response = await fetch(`${this.apiBaseUrl}/api/get-wasteking-prices`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        postcode: this.chatState.postcode,
                        service: this.chatState.serviceType,
                        customer_phone: 'web_chat',
                        agent_name: 'Thomas_Chat',
                        conversation_id: this.conversationId
                    })
                });

                const data = await response.json();
                
                if (data.status === 'success') {
                    this.chatState.quoteId = data.quote_id;
                    await this.delay(1500);
                    this.addBotMessage("Great news! I've got pricing available for your area.");
                    await this.delay(800);
                    this.addBotMessage("Are you looking to book today, or just getting prices? If you book with me today I can offer a £10 discount.");
                    this.chatState.step = 'booking_intent';
                } else {
                    await this.delay(1500);
                    this.addBotMessage(`Sorry, we don't currently service ${this.chatState.serviceType} in ${this.chatState.postcode}. Please contact us directly for special arrangements.`);
                    await this.delay(500);
                    this.addSystemMessage(`Call us: ${CONFIG.phoneNumber}`);
                }
            } catch (error) {
                console.error('Quote API error:', error);
                await this.delay(1500);
                this.addBotMessage("I'm having trouble accessing our pricing system. Please contact us directly for a quote.");
                this.addSystemMessage(`Call us: ${CONFIG.phoneNumber}`);
            }
        }

        async handleBookingIntent(message) {
            const lowerMessage = message.toLowerCase();
            
            if (lowerMessage.includes('book') || lowerMessage.includes('yes') || 
                lowerMessage.includes('today') || lowerMessage.includes('now')) {
                this.chatState.isBooking = true;
                await this.delay(500);
                this.addBotMessage("Excellent! I can process that booking and payment for you now with a £10 discount.");
                this.chatState.step = 'phone_confirmation';
                await this.delay(800);
                this.addBotMessage("Can you confirm the best phone number to send the payment link to?");
            } else {
                await this.delay(500);
                this.addBotMessage("No problem! Here are the pricing details for your area. When you're ready to book, just start a new chat and I'll be happy to help.");
                await this.delay(500);
                this.addSystemMessage("Quote reference: " + this.chatState.quoteId);
            }
        }

        async handlePhoneConfirmation(message) {
            const phone = message.trim();
            
            const phoneRegex = /^(\+44|0)[0-9\s\-]{10,}$/;
            if (!phoneRegex.test(phone)) {
                this.addBotMessage("That doesn't look like a valid UK phone number. Please provide a number like: 07123 456789 or 0800 123 4567");
                return;
            }

            this.chatState.customerPhone = phone;
            await this.delay(500);
            this.addBotMessage(`Perfect! I'm sending you a secure PayPal payment link to ${phone} via text message.`);
            
            await this.sendPaymentSMS();
        }

        async sendPaymentSMS() {
            try {
                const response = await fetch(`${this.apiBaseUrl}/api/send-payment-sms`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({
                        quote_id: this.chatState.quoteId,
                        customer_phone: this.chatState.customerPhone,
                        amount: '1.00'
                    })
                });

                const data = await response.json();
                
                if (data.status === 'success') {
                    await this.delay(1000);
                    this.addBotMessage("I've sent you a payment link via SMS for £1.00 (testing amount). Please check your phone and complete the payment when convenient.");
                    await this.delay(800);
                    this.addBotMessage("Once you've completed the payment, please start a new chat and we'll finalize your booking immediately.");
                    await this.delay(500);
                    this.addBotMessage(`Your quote reference is ${this.chatState.quoteId} for your records. Thank you for choosing Waste King!`);
                } else {
                    this.addBotMessage("I'm having trouble sending the SMS. Please contact us directly to complete your booking.");
                    this.addSystemMessage(`Call us: ${CONFIG.phoneNumber}`);
                }
            } catch (error) {
                console.error('SMS API error:', error);
                this.addBotMessage("I'm having trouble sending the SMS. Please contact us directly to complete your booking.");
                this.addSystemMessage(`Call us: ${CONFIG.phoneNumber}`);
            }
        }

        async handleGeneralMessage(message) {
            const lowerMessage = message.toLowerCase();
            
            if (lowerMessage.includes('price') || lowerMessage.includes('cost') || lowerMessage.includes('quote')) {
                this.addBotMessage("I'd be happy to get you a quote! Let me start by asking what service you need.");
                this.chatState.step = 'greeting';
                await this.handleGreeting('I need a quote');
            } else if (lowerMessage.includes('help') || lowerMessage.includes('info')) {
                this.addBotMessage("I'm here to help with skip hire, Man & Van services, and grab hire. What can I help you with today?");
            } else {
                this.addBotMessage("I'm not sure I understand. I can help you with waste removal services. Would you like a quote for skip hire, Man & Van, or grab hire?");
            }
        }
    }

    // Initialize when DOM is ready
    function initChatbot() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', function() {
                window.wasteKingChatbot = new WasteKingChatbot();
                window.wasteKingChatbot.init();
            });
        } else {
            window.wasteKingChatbot = new WasteKingChatbot();
            window.wasteKingChatbot.init();
        }
    }

    // Start initialization
    initChatbot();

})();
