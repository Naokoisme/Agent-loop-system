# 小鯨鯨 Android 懸浮桌寵（v0.1）

原創藍髮鯨魚少女桌寵，非 DeepSeek 官方產品。源碼位於本倉庫 `whale-pet-android` **獨立分支**，不會合入原有主線。

## 本版功能
- 使用早前互動試玩版的九張 Q 版角色 PNG 動作圖；原始圖像以 Base64 存放於 `artwork/`，Gradle 編譯前會自動還原成 PNG。
- 透明懸浮窗，可拖動、點擊互動、跳躍、隨機走動、長按睡覺。
- 開啟網易雲音樂（裝置上已安裝時）。
- 可選的 Accessibility Service：只在使用者按鈕觸發時執行返回、回桌面、滑動、按可見文字點擊；按可見文字點擊須二次確認。

## 權限與限制
- 首次啟動需要授予「顯示在其他應用程式上層」；啟用手機控制時需另外到 Android 設定手動開啟無障礙服務。
- Android 13+ 可能要求通知權限；Android 14+ 對後台服務及側載 Accessibility 有額外限制，依手機品牌而異。
- 不收集或上傳畫面內容；不設置遠端控制通道；禁止用作密碼、支付、銀行等敏感操作。
- **尚未整合 ChatGPT 語音控制，亦不是可自動完成任何手機操作的 AI 代理。**
- 此版的確切兼容性與 APK 可安裝性要以 GitHub Actions 編譯及真機測試為準。

## GitHub Actions 雲端編譯
由 `.github/workflows/build-whale-pet-android.yml` 執行 Ubuntu + JDK 17 + Gradle 8.9 + Android SDK 編譯，輸出 artifact 名稱為 `whale-pet-debug-apk`。

[查看編譯工作流程](https://github.com/Naokoisme/Agent-loop-system/actions/workflows/build-whale-pet-android.yml)

到 Actions 頁面選擇成功的 `Build Whale Pet Android APK` 執行結果，再下載 artifact ZIP，解壓後得到 `app-debug.apk`。若 GitHub App 無法觸發 Actions，需倉庫管理員在 GitHub 網站檢查 Actions 設定和執行紀錄。

## 本地建置（可選）
在 `whale-pet/android` 內使用 Android Studio，或在具備 JDK 17、Android SDK 35 和 Gradle 8.9 的環境執行：
```bash
gradle :app:assembleDebug
```
產物位於 `app/build/outputs/apk/debug/app-debug.apk`。
