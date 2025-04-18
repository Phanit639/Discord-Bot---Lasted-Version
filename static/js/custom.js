/**
 * Discord Bot Management System
 * ไฟล์ JavaScript เพิ่มเติมสำหรับฟังก์ชันทั่วไป
 */

// ฟังก์ชันทำให้ข้อความเข้ากับมาร์กดาวน์ Discord
function formatDiscordMarkdown(text) {
    // แปลง Bold
    text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
    
    // แปลง Italic
    text = text.replace(/\*(.*?)\*/g, '<em>$1</em>');
    
    // แปลง Underline
    text = text.replace(/__(.*?)__/g, '<u>$1</u>');
    
    // แปลง Strikethrough
    text = text.replace(/~~(.*?)~~/g, '<del>$1</del>');
    
    // แปลง Code Blocks
    text = text.replace(/```(.*?)```/gs, '<pre><code>$1</code></pre>');
    
    // แปลง Inline Code
    text = text.replace(/`(.*?)`/g, '<code>$1</code>');
    
    // แปลง Blockquote
    text = text.replace(/^>(.*)/gm, '<blockquote>$1</blockquote>');
    
    // แปลง Links
    text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');
    
    return text;
}

// ฟังก์ชันแสดงข้อความแจ้งเตือน
function showNotification(message, type = 'info', timeout = 3000) {
    const alertClass = type === 'success' ? 'alert-success' : 
                        type === 'error' ? 'alert-danger' : 
                        type === 'warning' ? 'alert-warning' : 'alert-info';
    
    const alertHtml = `
        <div class="alert ${alertClass} alert-dismissible fade show notification-alert" role="alert">
            ${message}
            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
        </div>
    `;
    
    // เพิ่ม alert ไว้ด้านบนของหน้า
    const alertContainer = $('#alert-container');
    if (alertContainer.length) {
        alertContainer.append(alertHtml);
    } else {
        $('body').prepend(`<div id="alert-container" style="position: fixed; top: 20px; right: 20px; z-index: 9999;">${alertHtml}</div>`);
    }
    
    // ซ่อนอัตโนมัติหลังจากเวลาที่กำหนด
    if (timeout > 0) {
        setTimeout(() => {
            $('.notification-alert').alert('close');
        }, timeout);
    }
}

// ฟังก์ชันแปลงซิงค์และอัปเดตข้อมูลจาก API
function syncData(url, callback, errorCallback) {
    $.ajax({
        url: url,
        type: 'GET',
        dataType: 'json',
        success: function(response) {
            if (callback) callback(response);
        },
        error: function(xhr, status, error) {
            console.error('Error fetching data:', error);
            if (errorCallback) errorCallback(error);
        }
    });
}

// ฟังก์ชันตรวจสอบการเชื่อมต่อกับเซิร์ฟเวอร์
function checkServerConnection() {
    return new Promise((resolve, reject) => {
        $.ajax({
            url: '/api/server-status',
            type: 'GET',
            timeout: 3000,
            success: function(response) {
                resolve(response.online === true);
            },
            error: function() {
                resolve(false);
            }
        });
    });
}

// เพิ่มฟังก์ชันช่วยเหลือสำหรับการจัดการฟอร์ม
function handleFormSubmission(formId, successCallback, errorCallback) {
    $(`#${formId}`).submit(function(e) {
        e.preventDefault();
        
        const form = $(this);
        const submitButton = form.find('button[type="submit"]');
        const originalButtonText = submitButton.html();
        
        // แสดงสถานะโหลด
        submitButton.html('<i class="fas fa-spinner fa-spin"></i> กำลังบันทึก...');
        submitButton.prop('disabled', true);
        
        $.ajax({
            url: form.attr('action'),
            type: form.attr('method'),
            data: form.serialize(),
            success: function(response) {
                if (successCallback) successCallback(response);
                
                // คืนค่าปุ่มกลับ
                submitButton.html(originalButtonText);
                submitButton.prop('disabled', false);
            },
            error: function(xhr, status, error) {
                if (errorCallback) errorCallback(xhr, status, error);
                
                // คืนค่าปุ่มกลับ
                submitButton.html(originalButtonText);
                submitButton.prop('disabled', false);
            }
        });
    });
}

// เตรียมพร้อมเมื่อโหลดหน้าเสร็จ
$(document).ready(function() {
    // เพิ่มคลาสแอนิเมชันเมื่อโหลดหน้าเสร็จ
    $('.content').addClass('fade-in');
    
    // เพิ่มการตรวจจับการคลิกเมนูในโหมดมือถือ
    $('.navbar-nav .nav-link').on('click', function() {
        if ($('.navbar-toggler').is(':visible')) {
            $('.navbar-toggler').click();
        }
    });
    
    // ตรวจสอบการเชื่อมต่อทุก 30 วินาที
    setInterval(async function() {
        const isConnected = await checkServerConnection();
        if (!isConnected) {
            showNotification('การเชื่อมต่อกับเซิร์ฟเวอร์ขัดข้อง กำลังพยายามเชื่อมต่อใหม่...', 'warning', 0);
        } else {
            $('#alert-container .notification-alert').alert('close');
        }
    }, 30000);
});