#!/bin/bash

# 进入项目目录
cd /home/zuucha/Desktop/SmartGreenhouse

# 获取当前时间作为提交信息
TIME=$(date "+%Y-%m-%d %H:%M:%S")

# 执行 Git 操作
echo "Starting backup at $TIME..."
git add .
# 如果没有文件变化，git commit 会报错，所以加个判断
if git commit -m "Auto-backup: $TIME"; then
    echo "Changes detected, pushing to GitHub..."
    git push origin main
else
    echo "No changes to commit."
fi
echo "Backup finished."
