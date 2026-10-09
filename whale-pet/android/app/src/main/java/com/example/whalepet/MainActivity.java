package com.example.whalepet;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.widget.*;
import android.view.View;

public class MainActivity extends Activity {
  private TextView status;
  private final Handler handler=new Handler(Looper.getMainLooper());
  private int dp(float n){return (int)(n*getResources().getDisplayMetrics().density+0.5f);}
  private Button button(LinearLayout parent,String label,View.OnClickListener run){
    Button b=new Button(this);b.setText(label);parent.addView(b);b.setOnClickListener(run);return b;
  }
  @Override public void onCreate(Bundle state){
    super.onCreate(state);
    ScrollView scroll=new ScrollView(this);
    LinearLayout root=new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    root.setBackgroundColor(Color.rgb(236,247,255));
    root.setPadding(dp(24),dp(28),dp(24),dp(36));
    scroll.addView(root);setContentView(scroll);

    PetArtView mascot=new PetArtView(this);
    root.addView(mascot,new LinearLayout.LayoutParams(-1,dp(195)));
    TextView heading=new TextView(this);heading.setText("🐋 小鯨鯨 · 懸浮桌寵");
    heading.setGravity(Gravity.CENTER);heading.setTextSize(23);
    heading.setTextColor(0xff286da9);root.addView(heading);
    TextView caption=new TextView(this);
    caption.setText("拖動小鯨鯨自由移動，點一下會跳同賣萌，長按會瞓覺。\n手機控制功能需要你親自授權，並且逐次發出操作。");
    caption.setPadding(0,dp(10),0,dp(12));root.addView(caption);
    status=new TextView(this);root.addView(status);
    button(root,"✨ 開啟懸浮桌寵",v->startPet());
    button(root,"🌙 收起桌寵",v->{stopService(new Intent(this,PetService.class));status.setText("小鯨鯨已收起");});

    TextView title=new TextView(this);title.setText("📱 手機控制 · 可選");
    title.setTextSize(20);title.setPadding(0,dp(16),0,dp(8));root.addView(title);
    TextView caution=new TextView(this);
    caution.setText("以下操作只喺你按按鈕時執行；需由你喺 Android 系統設定啟用無障礙服務。唔會收集或者上傳畫面資料，請勿用於付款或密碼輸入。");
    root.addView(caution);
    button(root,"🔐 前往無障礙授權設定",v->startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)));
    button(root,"🎵 打開網易雲音樂",v->{
      Intent launch=getPackageManager().getLaunchIntentForPackage("com.netease.cloudmusic");
      if(launch==null){status.setText("搵唔到網易雲音樂，請先安裝。");}
      else {startActivity(launch);status.setText("已開啟網易雲音樂");}
    });
    button(root,"↩️ 返回上一頁",v->performAfterExit(WhaleAccessibilityService::back));
    button(root,"🏠 返回桌面",v->performAfterExit(WhaleAccessibilityService::home));
    button(root,"⬆️ 向上滑動一次",v->performAfterExit(WhaleAccessibilityService::swipeUp));
    EditText target=new EditText(this);
    target.setSingleLine(true);target.setHint("輸入要點擊嘅可見按鈕文字");
    root.addView(target);
    button(root,"👆 確認後點擊指定文字",v->{
      final String label=target.getText().toString().trim();
      if(label.isEmpty()){status.setText("請輸入按鈕文字");return;}
      for(String word:new String[]{"支付","付款","密碼","密码","銀行","银行","轉帳","转账","刪除","删除"}){
        if(label.contains(word)){status.setText("為安全起見，唔支援呢類敏感操作");return;}
      }
      new AlertDialog.Builder(this).setTitle("確認點擊？")
        .setMessage("你確認要喺上一個 App 嘗試點擊可見文字「"+label+"」？請勿用於財務或敏感操作。")
        .setNegativeButton("取消",null)
        .setPositiveButton("確認",(dialog,which)->performAfterExit(()->WhaleAccessibilityService.tapVisible(label)))
        .show();
    });
    TextView foot=new TextView(this);
    foot.setPadding(0,dp(18),0,0);foot.setTextSize(12);
    foot.setText("原創桌寵試玩版，並非 DeepSeek 官方程式。當前版本唔支援 ChatGPT 自動語音控制。");
    root.addView(foot);
    updateStatus();
  }
  private void startPet(){
    if(!Settings.canDrawOverlays(this)){
      startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,Uri.parse("package:"+getPackageName())));
      status.setText("請先授權懸浮窗，返回後再按開啟");return;
    }
    if(Build.VERSION.SDK_INT>=33&&checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED){
      requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},88);
    }
    try{
      if(Build.VERSION.SDK_INT>=26)startForegroundService(new Intent(this,PetService.class));
      else startService(new Intent(this,PetService.class));
      status.setText("✅ 小鯨鯨啟動中，返桌面睇下！");
    }catch(Exception e){status.setText("啟動失敗，請檢查背景服務權限");}
  }
  private interface Command{boolean run();}
  private void performAfterExit(Command cmd){
    if(!WhaleAccessibilityService.ready()){
      status.setText("⚠️ 請先喺系統設定開啟小鯨鯨無障礙服務");return;
    }
    status.setText("已發出操作，返回之前嘅畫面");
    moveTaskToBack(true);
    handler.postDelayed(()->cmd.run(),700);
  }
  private void updateStatus(){
    if(status!=null)status.setText(Settings.canDrawOverlays(this)?"✅ 已授權懸浮窗":"⚠️ 尚未授權懸浮窗");
  }
  @Override protected void onResume(){super.onResume();updateStatus();}
  @Override protected void onDestroy(){handler.removeCallbacksAndMessages(null);super.onDestroy();}
}
