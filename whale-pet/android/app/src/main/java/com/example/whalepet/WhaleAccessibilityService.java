package com.example.whalepet;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Handler;
import android.os.Looper;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;

/** Local opt-in helper. Never sends UI data to a server. */
public class WhaleAccessibilityService extends AccessibilityService {
  private static WhaleAccessibilityService instance;
  @Override protected void onServiceConnected(){super.onServiceConnected();instance=this;}
  @Override public void onAccessibilityEvent(AccessibilityEvent event){}
  @Override public void onInterrupt(){}
  @Override public void onDestroy(){if(instance==this)instance=null;super.onDestroy();}
  public static boolean ready(){return instance!=null;}
  public static boolean back(){return ready()&&instance.performGlobalAction(GLOBAL_ACTION_BACK);}
  public static boolean home(){return ready()&&instance.performGlobalAction(GLOBAL_ACTION_HOME);}
  public static boolean swipeUp(){
    if(!ready())return false;
    int w=instance.getResources().getDisplayMetrics().widthPixels;
    int h=instance.getResources().getDisplayMetrics().heightPixels;
    Path p=new Path();p.moveTo(w*.5f,h*.78f);p.lineTo(w*.5f,h*.3f);
    GestureDescription gesture=new GestureDescription.Builder()
       .addStroke(new GestureDescription.StrokeDescription(p,0,420)).build();
    return instance.dispatchGesture(gesture,null,new Handler(Looper.getMainLooper()));
  }
  public static boolean tapVisible(String label){
    if(!ready()||label==null||label.trim().isEmpty()||label.length()>80)return false;
    AccessibilityNodeInfo root=instance.getRootInActiveWindow();
    if(root==null)return false;
    try{return findAndClick(root,label.trim(),0);}finally{root.recycle();}
  }
  private static boolean findAndClick(AccessibilityNodeInfo node,String label,int depth){
    if(node==null||depth>40)return false;
    CharSequence text=node.getText(),desc=node.getContentDescription();
    if((text!=null&&label.contentEquals(text))||(desc!=null&&label.contentEquals(desc))){
      AccessibilityNodeInfo curr=node;
      for(int i=0;i<8&&curr!=null;i++){
        if(curr.isVisibleToUser()&&curr.isClickable())return curr.performAction(AccessibilityNodeInfo.ACTION_CLICK);
        curr=curr.getParent();
      }
    }
    for(int i=0;i<node.getChildCount();i++){
      AccessibilityNodeInfo child=node.getChild(i);
      if(child!=null){
        boolean done=findAndClick(child,label,depth+1);
        child.recycle();
        if(done)return true;
      }
    }
    return false;
  }
}
