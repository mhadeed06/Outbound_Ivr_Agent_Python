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

# All insurance configurations
INSURANCE_CONFIGS = {
    "CIGNA": InsuranceConfig(
        name="CIGNA",
        phone_number="+18009971654",
        debounce_seconds=0.5,
        claim_debounce_seconds=1.4,
        claims_tail_chars=250,
        prompt_template="CIGNA_PROMPT_TEMPLATE",
        claims_prompt_template="CIGNA_CLAIMS_CONTROLLER_TEMPLATE"
    ),
    
    "HUMANA": InsuranceConfig(
        name="HUMANA", 
        phone_number="+18007834599",
        debounce_seconds=0.4,
        claim_debounce_seconds=1.2,
        claims_tail_chars=150,
        prompt_template="HUMANA_PROMPT_TEMPLATE",
        claims_prompt_template="HUMANA_CLAIMS_CONTROLLER_TEMPLATE"
    ),
    
    "BAYLOR_SCOTT": InsuranceConfig(
        name="BAYLOR_SCOTT",
        phone_number="+18555727238", 
        debounce_seconds=0.1,
        claim_debounce_seconds=1.2,
        claims_tail_chars=200,
        prompt_template="BAYLOR_SCOTT_PROMPT_TEMPLATE", 
        claims_prompt_template="BAYLOR_SCOTT_CLAIMS_CONTROLLER_TEMPLATE"
    )
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

# Global instance
config_manager = ConfigManager()