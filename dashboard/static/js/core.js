/**
 * Core Utilities for Dashboard
 */

/**
 * Prompts the user for an admin password.
 * @param {string} actionName - Name of the action being performed (e.g., "delete this file")
 * @returns {string|null} - The entered password, or null if cancelled.
 */
function getAdminPassword(actionName) {
    return prompt(`请输入管理密码以确认${actionName}：`);
}
