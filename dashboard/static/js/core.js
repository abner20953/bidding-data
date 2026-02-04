/**
 * Core Utilities for Dashboard
 */

const ADMIN_PASSWORD = '108';

/**
 * Prompts the user for an admin password and verifies it.
 * @param {string} actionName - Name of the action being performed (e.g., "delete this file")
 * @returns {boolean} - True if password is correct, false otherwise.
 */
function verifyAdminPassword(actionName) {
    const pwd = prompt(`请输入管理密码以确认${actionName}：`);
    if (pwd === ADMIN_PASSWORD) {
        return true;
    } else {
        if (pwd !== null) { // Don't alert if user clicked cancel (which returns null)
             alert('密码错误，操作取消');
        }
        return false;
    }
}
