# -*- coding: utf-8 -*-
"""API-27 二次验证（Challenge 档）

决策为「二次验证」时生成待验证记录并阻断当前操作；
验证通过放行、超时未验证则升级为人工审核（PRD FR-04 / SPEC Q10）。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import ChallengeVerifyRequest
from app.service import case_service

router = APIRouter(prefix="/challenge", tags=["二次验证"])


@router.post("/{challenge_id}/verify", summary="提交二次验证（API-27）",
             description="模拟实现：不接真实短信/验证码通道，凭据只做等值比对。"
                         "验证通过不创建案件；超时未验证会自动升级为人工审核。")
def verify_challenge(challenge_id: str, payload: ChallengeVerifyRequest,
                     db: Session = Depends(get_db)) -> dict:
    return case_service.verify_challenge(db, challenge_id, payload.verify_token)


@router.post("/expire-scan", summary="手动扫描超时验证并升级（运维）",
             description="等同于定时任务：把所有已超时且仍为「待验证」的记录置为"
                         "「已超时」并创建案件（复用同一 assessment_id）。")
def expire_scan(db: Session = Depends(get_db)) -> dict:
    n = case_service.expire_challenges(db)
    return {"escalated": n, "message": f"{n} 条超时验证已升级为人工审核"}
