package com.example.whalepet;

import android.app.*;
import android.content.*;
import android.content.pm.ServiceInfo;
import android.graphics.PixelFormat;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.*;
import android.view.*;
import android.widget.*;
import java.util.Random;

public class PetService extends Service {
  private static final String CHANNEL="whale_pet";
  private final Handler handler=new Handler(Looper.getMainLooper());
  private final Random rng=new Random();
  private WindowManager manager;
  private WindowManager.LayoutParams position;
  private FrameLayout overlay;
  private PetArtView art;
  private TextView speech;
  private boolean dragging=false;
  private float startX,startY;
  private int originalX,originalY,cycle=0;
  private long downAt;
  private final String[] messages={"跳跳！","食飯時間！","你好呀～","哇！","害羞了～","好眼瞓～","散步中～"};
  @Override public void onCreate(){
    super.onCreate();
    NotificationManager nm=getSystemService(NotificationManager.class);
    nm.createNotificationChannel(new NotificationChannel(CHANNEL,"小鯨鯨桌寵",NotificationManager.IMPORTANCE_LOW));
    PendingIntent open=PendingIntent.getActivity(this,0,new Intent(this,MainActivity.class),
        PendingIntent.FLAG_IMMUTABLE|PendingIntent.FLAG_UPDATE_CURRENT);
    Notification note=new Notification.Builder(this,CHANNEL)
        .setContentTitle("小鯨鯨喺度陪住你").setContentText("點擊返回設定")
        .setContentIntent(open).setSmallIcon(R.drawable.ic_whale).setOngoing(true).build();
    if(Build.VERSION.SDK_INT>=34)startForeground(9,note,ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
    else startForeground(9,note);
    showOverlay();
    handler.postDelayed(roam,4200);
  }
  @Override public int onStartCommand(Intent i,int f,int id){return START_STICKY;}
  private final Runnable roam=new Runnable(){
    @Override public void run(){
      if(overlay!=null&&!dragging){
        if(rng.nextInt(3)==0){
          react(6,900);
          int width=getResources().getDisplayMetrics().widthPixels;
          position.x=Math.min(Math.max(0,width-dp(175)),Math.max(0,position.x+(rng.nextBoolean()?dp(23):-dp(23))));
          try{manager.updateViewLayout(overlay,position);}catch(Exception ignored){}
        }else if(rng.nextInt(3)==0)hop();
        else react(4,1000);
      }
      handler.postDelayed(this,3500+rng.nextInt(3000));
    }
  };
  private void showOverlay(){
    manager=(WindowManager)getSystemService(WINDOW_SERVICE);
    position=new WindowManager.LayoutParams(dp(185),dp(220),
       WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
       WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
       PixelFormat.TRANSLUCENT);
    position.gravity=Gravity.TOP|Gravity.LEFT;position.x=dp(25);position.y=dp(150);
    overlay=new FrameLayout(this);
    art=new PetArtView(this);
    overlay.addView(art,new FrameLayout.LayoutParams(dp(182),dp(198),Gravity.BOTTOM|Gravity.CENTER_HORIZONTAL));
    speech=new TextView(this);
    speech.setTextSize(12);speech.setTextColor(Color.rgb(23,81,143));speech.setPadding(dp(9),dp(5),dp(9),dp(5));
    GradientDrawable bubble=new GradientDrawable();bubble.setColor(0xeeffffff);bubble.setCornerRadius(dp(16));speech.setBackground(bubble);
    overlay.addView(speech,new FrameLayout.LayoutParams(-2,-2,Gravity.TOP|Gravity.CENTER_HORIZONTAL));
    speech.setVisibility(View.INVISIBLE);
    overlay.setOnTouchListener((view,event)->{
      switch(event.getActionMasked()){
        case MotionEvent.ACTION_DOWN:
          startX=event.getRawX();startY=event.getRawY();
          originalX=position.x;originalY=position.y;downAt=System.currentTimeMillis();dragging=false;
          return true;
        case MotionEvent.ACTION_MOVE:
          float dx=event.getRawX()-startX,dy=event.getRawY()-startY;
          if(Math.hypot(dx,dy)>dp(8))dragging=true;
          if(dragging){position.x=originalX+(int)dx;position.y=originalY+(int)dy;
            try{manager.updateViewLayout(overlay,position);}catch(Exception ignored){}}
          return true;
        case MotionEvent.ACTION_UP:
          if(!dragging){
            if(System.currentTimeMillis()-downAt>650)react(5,2500);
            else{int next=cycle++%5;react(next,1250);if(next==0)hop();}
          }else react(4,650);
          dragging=false;return true;
        default:return true;
      }
    });
    manager.addView(overlay,position);
  }
  private void react(int pose,int duration){
    if(art==null)return;
    art.setPose(pose);speech.setText(messages[Math.min(pose,messages.length-1)]);
    speech.setVisibility(View.VISIBLE);
    handler.postDelayed(()->{if(art!=null){art.setPose(0);speech.setVisibility(View.INVISIBLE);}},duration);
  }
  private void hop(){
    if(art==null)return;
    art.animate().translationY(-dp(22)).setDuration(180)
       .withEndAction(()->{if(art!=null)art.animate().translationY(0).setDuration(220).start();}).start();
  }
  private int dp(float px){return (int)(px*getResources().getDisplayMetrics().density+0.5f);}
  @Override public void onDestroy(){
    handler.removeCallbacksAndMessages(null);
    if(art!=null)art.animate().cancel();
    if(overlay!=null&&manager!=null)try{manager.removeView(overlay);}catch(Exception ignored){}
    overlay=null;art=null;super.onDestroy();
  }
  @Override public IBinder onBind(Intent intent){return null;}
}
