package com.example.whalepet;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.view.View;

/** Original hand-drawn-by-code chibi whale girl. No external images or network needed. */
public class PetArtView extends View {
    private final Paint pen = new Paint(Paint.ANTI_ALIAS_FLAG);
    private int pose = 0;
    public PetArtView(Context context){ super(context); setLayerType(View.LAYER_TYPE_SOFTWARE, null); }
    public void setPose(int value){ pose=value; invalidate(); }
    private void color(int argb){ pen.setColor(argb); pen.setStyle(Paint.Style.FILL); pen.setStrokeWidth(1); }
    private void ellipse(Canvas c,float l,float t,float r,float b,int col){
        color(col); c.drawOval(l,t,r,b,pen);
    }
    private void line(Canvas c,float x,float y,float xx,float yy,int col,float width){
        color(col);pen.setStyle(Paint.Style.STROKE);pen.setStrokeCap(Paint.Cap.ROUND);pen.setStrokeWidth(width);c.drawLine(x,y,xx,yy,pen);pen.setStyle(Paint.Style.FILL);
    }
    @Override protected void onDraw(Canvas c){
        super.onDraw(c);
        c.save();
        c.scale(getWidth()/200f,getHeight()/210f);
        int ink=Color.rgb(38,91,147), sea=Color.rgb(60,156,219), hair=Color.rgb(72,167,239);
        ellipse(c,31,193,170,206,0x33539BCC);
        // Whale tail flowing behind the hair.
        Path tail=new Path();
        tail.moveTo(148,104);tail.cubicTo(170,75,190,95,188,119);
        tail.cubicTo(195,102,207,110,196,133);tail.cubicTo(180,145,164,136,145,128);
        tail.close();color(sea);c.drawPath(tail,pen);
        ellipse(c,149,108,176,153,0xFF3889CE);
        // Hair silhouette, long twin fins and blue strands.
        ellipse(c,41,23,158,174,ink);
        ellipse(c,44,27,154,171,hair);
        ellipse(c,48,76,78,194,0xFF327BC9);
        ellipse(c,121,73,152,195,0xFF327BC9);
        // Little whale-fin hair clip.
        Path fin=new Path();
        fin.moveTo(121,27);fin.cubicTo(125,4,133,8,144,22);
        fin.cubicTo(157,3,162,15,151,38);fin.close();color(0xFF2D88D0);c.drawPath(fin,pen);
        // Dress and ribbon.
        ellipse(c,67,133,135,195,0xFF1D4E92);
        Path skirt=new Path();skirt.moveTo(78,143);skirt.lineTo(122,143);skirt.lineTo(150,191);skirt.lineTo(49,191);skirt.close();color(0xFF214D91);c.drawPath(skirt,pen);
        ellipse(c,81,135,121,167,0xFFF6F9FF);
        Path bow=new Path();bow.moveTo(99,151);bow.lineTo(83,143);bow.lineTo(83,159);
        bow.close();color(0xFF66BFFA);c.drawPath(bow,pen);
        Path bow2=new Path();bow2.moveTo(99,151);bow2.lineTo(115,142);bow2.lineTo(115,160);
        bow2.close();c.drawPath(bow2,pen);ellipse(c,95,148,103,156,0xFF2269B2);
        // Arms move based on reaction.
        if(pose==2){line(c,70,148,43,103,0xFFFBDCCA,15);ellipse(c,37,95,51,113,0xFFFBDCCA);}
        else{line(c,71,149,54,169,0xFFFBDCCA,14);ellipse(c,44,162,59,177,0xFFFBDCCA);}
        line(c,128,149,146,166,0xFFFBDCCA,14);
        // Head + fringe.
        ellipse(c,57,48,143,147,0xFFFFE6D4);
        ellipse(c,54,35,146,98,hair);
        Path bangs=new Path();bangs.moveTo(55,72);bangs.cubicTo(52,35,88,31,110,44);bangs.cubicTo(139,36,147,60,144,91);
        bangs.cubicTo(130,85,121,74,116,61);bangs.cubicTo(104,84,86,89,71,82);bangs.lineTo(55,72);bangs.close();color(hair);c.drawPath(bangs,pen);
        ellipse(c,63,105,78,116,0xFFFFAFC8);
        ellipse(c,123,105,138,116,0xFFFFAFC8);
        if(pose==5){
            line(c,76,102,88,102,ink,3);line(c,114,102,126,102,ink,3);
        }else if(pose==4){
            line(c,76,104,86,108,ink,3);line(c,86,108,94,102,ink,3);
            line(c,109,102,118,108,ink,3);line(c,118,108,127,104,ink,3);
        }else{
            ellipse(c,76,95,91,113,ink);ellipse(c,111,95,126,113,ink);
            ellipse(c,80,96,84,102,Color.WHITE);ellipse(c,114,96,118,102,Color.WHITE);
        }
        if(pose==3){
            ellipse(c,97,113,107,125,0xFFB65A83);
        }else{
            line(c,96,119,100,122,0xFFAB6580,2);
            line(c,100,122,105,118,0xFFAB6580,2);
        }
        // Tiny blue sparkle when delighted.
        if(pose==1||pose==2){
            line(c,153,43,153,59,0xFF39B6F0,3);line(c,146,51,161,51,0xFF39B6F0,3);
        }
        c.restore();
    }
}
