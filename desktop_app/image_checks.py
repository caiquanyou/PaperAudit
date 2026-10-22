"""Unified legacy exact/pHash, reciprocal ORB and multi-model SIFT operations.

Adapted from the supplied within_paper_image_reuse.py / verify_page_pair.py
and image_forensics.py. No legacy paths or CLI entry points are executed.
ORB/RANSAC ancestry is documented by the supplied code as gengscan (MIT).
"""
from __future__ import annotations

import hashlib
import itertools
import math

import cv2
import numpy as np
from PIL import Image


def gray(path):
    with Image.open(path) as image:
        return cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2GRAY)


def phash(image):
    dct = cv2.dct(cv2.resize(image,(32,32)).astype(np.float32))[:8,:8]
    threshold = float(np.median(dct.flatten()[1:]))
    value = 0
    for bit in (dct>threshold).flatten():
        value = (value<<1)|int(bit)
    return value


def variations(image):
    return [("none",image),("hflip",cv2.flip(image,1)),("vflip",cv2.flip(image,0)),("rotate90",cv2.rotate(image,cv2.ROTATE_90_CLOCKWISE)),("rotate180",cv2.rotate(image,cv2.ROTATE_180)),("rotate270",cv2.rotate(image,cv2.ROTATE_90_COUNTERCLOCKWISE))]


def original_points(points, mode, shape):
    h,w = shape[:2]
    p = points.copy()
    x,y = points[:,0],points[:,1]
    if mode == "hflip": p[:,0]=w-1-x
    elif mode == "vflip": p[:,1]=h-1-y
    elif mode == "rotate90": p[:,0],p[:,1]=y,h-1-x
    elif mode == "rotate180": p[:,0],p[:,1]=w-1-x,h-1-y
    elif mode == "rotate270": p[:,0],p[:,1]=w-1-y,x
    return p


def normalized_box(points, shape):
    h,w=shape[:2]
    return [max(0,float(points[:,0].min()-6)/w),max(0,float(points[:,1].min()-6)/h),min(1,float(points[:,0].max()+7)/w),min(1,float(points[:,1].max()+7)/h)]


def anchor(record, box=None):
    bounds=record["bbox"]
    box=box or [0,0,1,1]
    return {"artifact_id":record["artifact_id"],"page":record["page"],"bbox":[bounds[0]+box[0]*(bounds[2]-bounds[0]),bounds[1]+box[1]*(bounds[3]-bounds[1]),bounds[0]+box[2]*(bounds[2]-bounds[0]),bounds[1]+box[3]*(bounds[3]-bounds[1])],"occurrence_id":record["id"]}


def same_display_region(left,right):
    return left["artifact_id"]==right["artifact_id"] and left.get("page")==right.get("page") and all(abs(a-b)<.001 for a,b in zip(left.get("bbox",[0,0,1,1]),right.get("bbox",[0,0,1,1])))


def pixel_support(left,right,transform,source_points,target_points):
    x,y,w,h=cv2.boundingRect(target_points.astype(np.float32))
    x,y=max(0,x),max(0,y)
    w,h=min(w,right.shape[1]-x),min(h,right.shape[0]-y)
    if w<15 or h<15:
        return None
    warped=cv2.warpAffine(left,transform,(right.shape[1],right.shape[0]))
    mask=np.zeros((h,w),np.uint8)
    points=target_points-np.array([x,y],np.float32)
    cv2.fillConvexPoly(mask,cv2.convexHull(points.astype(np.int32)),255)
    valid=mask.astype(bool)
    a=warped[y:y+h,x:x+w][valid].astype(float)
    b=right[y:y+h,x:x+w][valid].astype(float)
    if len(a)<200 or a.std()<4 or b.std()<4:
        return None
    correlation=float(np.corrcoef(a,b)[0,1])
    return correlation if math.isfinite(correlation) else None


class Features:
    def __init__(self, images):
        self.pixels={r["id"]:gray(r["path"]) for r in images}
        self.cache={}

    def get(self, record, method, mode="none"):
        key=(record["id"],method,mode)
        if key not in self.cache:
            image=dict(variations(self.pixels[record["id"]]))[mode]
            detector=cv2.ORB_create(nfeatures=1600) if method=="ORB" else cv2.SIFT_create(nfeatures=3000,contrastThreshold=.01)
            kp,desc=detector.detectAndCompute(image,None)
            self.cache[key]=(image,kp,desc)
        return self.cache[key]


def geometric_matches(features,left_record,right_record,method="ORB",mode="none",max_models=1):
    left,lk,ld=features.get(left_record,method)
    right,rk,rd=features.get(right_record,method,mode)
    if ld is None or rd is None or len(lk)<12 or len(rk)<12:
        return []
    matcher=cv2.BFMatcher(cv2.NORM_HAMMING if method=="ORB" else cv2.NORM_L2)
    fwd=matcher.knnMatch(ld,rd,k=2)
    rev=matcher.knnMatch(rd,ld,k=2)
    reverse={v[0].queryIdx:v[0].trainIdx for v in rev if len(v)==2 and v[0].distance<.75*v[1].distance}
    matches=[v[0] for v in fwd if len(v)==2 and v[0].distance<.75*v[1].distance and reverse.get(v[0].trainIdx)==v[0].queryIdx]
    results=[]
    for _ in range(max_models):
        if len(matches)<12:
            break
        sp=np.float32([lk[m.queryIdx].pt for m in matches])
        tp=np.float32([rk[m.trainIdx].pt for m in matches])
        transform,mask=cv2.estimateAffinePartial2D(sp,tp,method=cv2.RANSAC,ransacReprojThreshold=3,maxIters=5000,confidence=.995)
        if transform is None or mask is None:
            break
        keep=mask.ravel().astype(bool)
        count=int(keep.sum())
        if count<12:
            break
        source,target=sp[keep],tp[keep]
        scale=float(np.hypot(transform[0,0],transform[0,1]))
        corr=pixel_support(left,right,transform,source,target)
        sb,tb=cv2.boundingRect(source),cv2.boundingRect(target)
        local=(min(sb[2:])>=28 and min(tb[2:])>=28 and max(sb[2:])/max(1,min(sb[2:]))<=8 and max(tb[2:])/max(1,min(tb[2:]))<=8)
        margin=all(b[1]+b[3]<.08*im.shape[0] or b[1]>.88*im.shape[0] for b,im in [(sb,left),(tb,right)])
        if local and not margin and .15<=scale<=6 and corr is not None and corr>=.85:
            original=original_points(target,mode,features.pixels[right_record["id"]].shape)
            results.append({"left_box":normalized_box(source,left.shape),"right_box":normalized_box(original,features.pixels[right_record["id"]].shape),"metrics":{"method":method+"/reciprocal/RANSAC/pixel","inliers":count,"matches":len(matches),"pixel_correlation":round(corr,5),"scale":round(scale,5),"right_transform":mode}})
        matches=[m for i,m in enumerate(matches) if not keep[i]]
    return results


def self_copy(features,record):
    image,kp,desc=features.get(record,"ORB")
    if desc is None or len(kp)<30:
        return None
    rows=cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc,desc,k=min(4,len(kp)))
    matches=[]
    targets=set()
    for candidates in rows:
        for m in candidates:
            if m.queryIdx>=m.trainIdx or m.distance>24 or m.trainIdx in targets:
                continue
            p,q=np.array(kp[m.queryIdx].pt),np.array(kp[m.trainIdx].pt)
            if np.linalg.norm(p-q)<50:
                continue
            targets.add(m.trainIdx)
            matches.append(m)
            break
    if len(matches)<20:
        return None
    sp=np.float32([kp[m.queryIdx].pt for m in matches]); tp=np.float32([kp[m.trainIdx].pt for m in matches])
    transform,mask=cv2.estimateAffinePartial2D(sp,tp,method=cv2.RANSAC,ransacReprojThreshold=3,maxIters=5000)
    if transform is None or mask is None or int(mask.sum())<20:
        return None
    keep=mask.ravel().astype(bool); sp,tp=sp[keep],tp[keep]
    corr=pixel_support(image,image,transform,sp,tp)
    left,right=normalized_box(sp,image.shape),normalized_box(tp,image.shape)
    area=lambda b:(b[2]-b[0])*(b[3]-b[1])
    intersection=max(0,min(left[2],right[2])-max(left[0],right[0]))*max(0,min(left[3],right[3])-max(left[1],right[1]))
    if corr is None or corr<.9 or min(area(left),area(right))<.01 or intersection/max(min(area(left),area(right)),1e-9)>.15:
        return None
    return {"left_box":left,"right_box":right,"metrics":{"method":"self-copy/ORB/RANSAC/pixel","inliers":int(keep.sum()),"pixel_correlation":round(corr,5)}}


def run_image_checks(images, progress=lambda *_:None, cancelled=lambda:False, pair_budget=160, page_budget=60):
    if not images:
        return [],[{"name":"图像候选与几何复核","status":"insufficient_data","reason":"没有可解析的图像/页面"}]
    cv2.setNumThreads(2)
    cv2.setRNGSeed(20261021)
    features=Features(images)
    findings=[]
    local=[r for r in images if r["kind"]!="page"]
    pages=[r for r in images if r["kind"]=="page"]
    counts={"image_pairs":0,"page_pairs":0,"self_images":0,"same_region_deduplicated":0}
    def emit(left,right,result,title):
        left_anchor,right_anchor=anchor(left,result.get("left_box")),anchor(right,result.get("right_box"))
        if same_display_region(left_anchor,right_anchor):
            counts["same_region_deduplicated"]+=1
            return
        findings.append({"title":title,"kind":"image","severity":"建议复核","evidence_level":"L2" if result["metrics"].get("exact_pixels") else "L1","rule_id":"image_reuse","observation":"在实际材料中发现区域对应候选。复核指标：" + str(result["metrics"]),"alternative_explanation":"可能为已披露共用对照、相同视野通道、同一源图、图版组装或排版元素；需结合图注和实验条件核对。","recommendation":"对照两端图注、实验分组及未裁剪原图；自动相似性不代表造假。","anchors":[anchor(left,result.get("left_box")),anchor(right,result.get("right_box"))],"methods":[result["metrics"]["method"]],"metrics":result["metrics"]})
    self_records=local+pages
    for record in self_records[:80]:
        if cancelled(): raise InterruptedError()
        result=self_copy(features,record)
        counts["self_images"]+=1
        if result: emit(record,record,result,"同图中存在局部复制对应候选")
    local_total=len(local)*(len(local)-1)//2
    for index,(left,right) in enumerate(itertools.combinations(local,2)):
        if index>=pair_budget: break
        if cancelled(): raise InterruptedError()
        counts["image_pairs"]+=1
        progress("local_checks",f"图像区域对比 {index+1}/{min(pair_budget,local_total)}")
        a,b=features.pixels[left["id"]],features.pixels[right["id"]]
        rgb_equal=False
        if a.shape==b.shape and np.array_equal(a,b):
            with Image.open(left["path"]) as li,Image.open(right["path"]) as ri:
                rgb_equal=np.array_equal(np.array(li.convert("RGB")),np.array(ri.convert("RGB")))
        if rgb_equal:
            emit(left,right,{"metrics":{"method":"display_crop_RGB/exact","exact_pixels":True,"scope":"rendered_crop_not_original_embedded_bytes"}},"不同位置出现相同显示裁图内容")
            continue
        # pHash is supplementary; ORB is run independently even if hashes differ.
        distance=min((phash(a)^phash(v)).bit_count() for _,v in variations(b))
        matches=[]
        for mode in ("none","hflip","vflip"):
            matches=geometric_matches(features,left,right,mode=mode)
            if matches: break
        if matches:
            matches[0]["metrics"]["phash_distance"]=distance
            emit(left,right,matches[0],"篇内图像区域存在几何与像素支持的对应候选")
        elif distance<=4 and a.std()>=5 and b.std()>=5:
            # A low-specificity lead remains a candidate, never a confirmed match.
            emit(left,right,{"metrics":{"method":"pHash/rotation-flip","hamming_distance":distance}},"图像感知哈希相近，尚未通过几何复核")
    page_total=len(pages)*(len(pages)-1)//2
    for index,(left,right) in enumerate(itertools.combinations(pages,2)):
        if index>=page_budget: break
        if cancelled(): raise InterruptedError()
        counts["page_pairs"]+=1
        progress("local_checks",f"页面SIFT局部复核 {index+1}/{min(page_budget,page_total)}")
        for result in geometric_matches(features,left,right,method="SIFT",max_models=5):
            emit(left,right,result,"页面局部存在几何与像素支持的对应候选")
    complete=counts["image_pairs"]==local_total and counts["page_pairs"]==page_total and counts["self_images"]==len(self_records)
    checks=[{"name":"图像候选与几何复核","status":"signal" if findings else "no_signal","coverage":"complete_within_index" if complete else "partial","reason":f"{len(local)}个图片出现位置，{len(pages)}个整页后备；已比对{counts}；图片对预算{pair_budget}、页对预算{page_budget}；页对每次最多5个模型；非全库检索。"}, {"name":"ELA/噪声伪造判定","status":"not_applicable","reason":"重压缩与噪声波动缺少伪造特异性，不单独产生结论；当前报告提供原件定位对照"}, {"name":"跨论文图像检索","status":"unavailable","reason":"本轮只执行当前论文及其附件内部检查，未建立授权语料快照"}]
    if not complete:
        checks[0]["reason"]+=" 未处理图像/页对已超限，零候选不可解释为不存在复用。"
    return findings,checks


def merge_findings(findings):
    merged=[]
    def iou(a,b):
        inter=max(0,min(a[2],b[2])-max(a[0],b[0]))*max(0,min(a[3],b[3])-max(a[1],b[1]))
        area=lambda x:(x[2]-x[0])*(x[3]-x[1])
        return inter/max(area(a)+area(b)-inter,1e-10)
    for item in findings:
        pair=item.get("anchors",[])
        if item.get("kind")=="image" and len(pair)==2 and same_display_region(*pair):
            continue
        existing=None
        if item.get("kind")=="image" and len(pair)==2:
            ordered=sorted(pair,key=lambda a:(a["artifact_id"],a.get("page",1),a.get("bbox",[0])[0]))
            for candidate in merged:
                other=candidate.get("anchors",[])
                if candidate.get("kind")!="image" or len(other)!=2: continue
                other=sorted(other,key=lambda a:(a["artifact_id"],a.get("page",1),a.get("bbox",[0])[0]))
                if all(a["artifact_id"]==b["artifact_id"] and a.get("page")==b.get("page") and iou(a.get("bbox",[0,0,1,1]),b.get("bbox",[0,0,1,1]))>.55 for a,b in zip(ordered,other)):
                    existing=candidate
                    break
        if existing:
            existing["methods"]=list(dict.fromkeys(existing.get("methods",[])+item.get("methods",[])))
            existing.setdefault("supporting_observations",[]).append(item["observation"])
        else:
            merged.append(item)
    return merged
