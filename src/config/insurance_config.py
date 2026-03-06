# insurance_config.py
from dataclasses import dataclass
from typing import Dict, Any
import os

@dataclass
class InsuranceConfig:
    """Configuration for each insurance provider"""
    name: str
    phone_number: str
    debounce_seconds: float
    claim_debounce_seconds: float
    claims_tail_chars: int
    prompt_template: str
    claims_prompt_template: str
    segmentation_silence_ms: int  # NEW: For normal flow
    claim_segmentation_silence_ms: int  # NEW: For claim flow


# All insurance configurations
INSURANCE_CONFIGS = {
    "CIGNA": InsuranceConfig(
        name="CIGNA",
        phone_number="+18009971654",
        debounce_seconds=0.5,
        claim_debounce_seconds=1.2,
        claims_tail_chars=250,
        prompt_template="CIGNA_PROMPT_TEMPLATE",
        claims_prompt_template="CIGNA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=600,
        claim_segmentation_silence_ms=1700
    ),
    
    "HUMANA": InsuranceConfig(
        name="HUMANA", 
        phone_number="+18007834599",
        debounce_seconds=0.4,
        claim_debounce_seconds=1.2,
        claims_tail_chars=150,
        prompt_template="HUMANA_PROMPT_TEMPLATE",
        claims_prompt_template="HUMANA_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=500,
        claim_segmentation_silence_ms=1300
    ),
    
    "BAYLOR_SCOTT": InsuranceConfig(
        name="BAYLOR_SCOTT",
        phone_number="+18555727238", 
        debounce_seconds=0.1,
        claim_debounce_seconds=1.7,
        claims_tail_chars=200,
        prompt_template="BAYLOR_SCOTT_PROMPT_TEMPLATE", 
        claims_prompt_template="BAYLOR_SCOTT_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=600,
        claim_segmentation_silence_ms=1000
    ),
    
    "OSCAR": InsuranceConfig(
        name="OSCAR",
        phone_number="+18556722755", 
        debounce_seconds=0.0,
        claim_debounce_seconds=1.2,
        claims_tail_chars=250,
        prompt_template="OSCAR_PROMPT_TEMPLATE", 
        claims_prompt_template="OSCAR_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1400,
        claim_segmentation_silence_ms=1800
    ),


    "HEALTH_FIRST": InsuranceConfig(
        name="HEALTH_FIRST",
        phone_number="+18882502220", 
        debounce_seconds=0.1,
        claim_debounce_seconds=1,
        claims_tail_chars=200,
        prompt_template="HEALTH_FIRST_PROMPT_TEMPLATE", 
        claims_prompt_template="HEALTH_FIRST_CLAIMS_CONTROLLER_TEMPLATE",
        segmentation_silence_ms=1400,
        claim_segmentation_silence_ms=1800
    ),
}

class ConfigManager:
    """Manages insurance configuration selection"""
    
    def __init__(self):
        self.current_config: InsuranceConfig = None
        self._load_config()
    
    def _load_config(self):
        """Load config based on environment variable"""
        insurance_type = os.getenv("INSURANCE_TYPE", "CIGNA").upper()
        
        if insurance_type not in INSURANCE_CONFIGS:
            raise ValueError(f"Unknown insurance type: {insurance_type}. Available: {list(INSURANCE_CONFIGS.keys())}")
            
        self.current_config = INSURANCE_CONFIGS[insurance_type]
        print(f"✅ Loaded configuration for: {self.current_config.name}")
    
    def get_config(self) -> InsuranceConfig:
        """Get current insurance configuration"""
        return self.current_config
    
    def get_phone_number(self) -> str:
        """Get phone number for current insurance"""
        return self.current_config.phone_number
    
    def get_debounce_seconds(self) -> float:
        """Get debounce seconds for current insurance"""
        return self.current_config.debounce_seconds
        
    def get_claim_debounce_seconds(self) -> float:
        """Get claim debounce seconds for current insurance"""
        return self.current_config.claim_debounce_seconds
    
    def get_claims_tail_chars(self) -> int:
        """Get claims tail chars for current insurance"""
        return self.current_config.claims_tail_chars
    
    def get_insurance_name(self) -> str:
        """Get current insurance name"""
        return self.current_config.name  
    
    def get_segmentation_silence_ms(self) -> int:
        """Get segmentation silence timeout for current insurance (normal flow)"""
        return self.current_config.segmentation_silence_ms
    
    def get_claim_segmentation_silence_ms(self) -> int:
        """Get segmentation silence timeout for current insurance (claim flow)"""
        return self.current_config.claim_segmentation_silence_ms



# Global instance
config_manager = ConfigManager()