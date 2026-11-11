CREATE DATABASE IF NOT EXISTS typeflow CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE typeflow;

CREATE TABLE IF NOT EXISTS user (
  id INT AUTO_INCREMENT PRIMARY KEY,
  username VARCHAR(32) NOT NULL UNIQUE,
  email VARCHAR(120) NOT NULL UNIQUE,
  password_hash VARCHAR(255) NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS typing_result (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT NOT NULL,
  mode VARCHAR(20) NOT NULL,
  duration INT NOT NULL,
  wpm DECIMAL(7,2) NOT NULL,
  raw_wpm DECIMAL(7,2) NOT NULL,
  accuracy DECIMAL(5,2) NOT NULL,
  correct_chars INT DEFAULT 0,
  incorrect_chars INT DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE,
  INDEX idx_result_user_date (user_id, created_at),
  INDEX idx_result_wpm (wpm)
);
