from pydantic import BaseModel
 
class SOW:
    prompt = """
    Extract below given key insights from provided data. Ensure all extracted items are meaningful text descriptions. Skip any items that are purely numeric data or internal ID codes.
        - Project objectives and high-level goals (descriptive text)
        - Project timeline and major phases
        - Detailed scope broken into 5 main phases
        - Specific deliverables with due dates
        - Roles and responsibilities (identify by designation/role)
        - Acceptance criteria for quality assurance
        - Risk management strategies
        - Payment terms and billing schedule
    """
 
class WSR:
    prompt = """
    Extract key descriptive insights from the provided Weekly Status Report (WSR). IMPORTANT: Do not extract raw numeric tables or calculation cells as 'stakeholders' or 'objectives'.
        - Project overview and reporting period
        - Overall project status (On Track, At Risk, Delayed)
        - Key accomplishments and milestones achieved this week (bullet points)
        - Work in progress and current activities
        - Upcoming tasks and planned activities for next week
        - Risks and issues identified (with descriptions)
        - Blockers and dependencies
        - Resource utilization and team status (List names or roles, skip numeric utilization percentages)
        - Budget and timeline status (Summary text only)
        - Action items and decisions needed
    """
 
class TechReview:
    prompt = """
    Analyze the provided technical document and extract key insights for a Technical Review Report:
        - Proposed Architecture and Design Patterns (e.g., Microservices vs Monolith)
        - Technology Stack Assessment (Suitability of languages, frameworks, and databases)
        - Scalability and High Availability Strategy (Load balancing, caching, replication)
        - Integration Interfaces (API standards, external system dependencies)
        - Security and Compliance (Authentication, authorization, data encryption)
        - Infrastructure and DevOps (Deployment strategy, CI/CD, containerization)
        - Critical Technical Risks and Single Points of Failure
 
    """
 
class CodeQuality:
    prompt = """
    Analyze the provided code and extract key insights for a technical quality report:
        - Architectural structural analysis (modularity and separation of concerns)
        - Adherence to language-specific best practices (e.g., PEP8 for Python)
        - Potential security vulnerabilities and sensitive data exposure
        - Performance bottlenecks and algorithmic complexity issues
        - Error handling robustness and edge case coverage
        - Maintainability score and technical debt assessment
        - Actionable refactoring recommendations
    """
class BestPractices:
    prompt = """
    Analyze the provided Engineering Guidelines or Process Document and generate a Best Practices Audit Report:
        - Version Control Strategy: Evaluation of branching strategies (e.g., GitFlow, Trunk-based) and commit standards.
        - Testing Maturity: Assessment of required testing layers (Unit, Integration, E2E) and coverage requirements.
        - CI/CD & Automation: Analysis of deployment pipelines, automated checks, and release procedures.
        - Code Review Standards: Evaluation of the peer review process, checklists, and approval requirements.
        - Security & Compliance: Check for "Security by Design" principles, dependency scanning, and secret management policies.
        - Documentation Standards: Requirements for API docs (Swagger), Readmes, and architecture decision records (ADRs).
        - GAP ANALYSIS: Identify missing industry standards or areas where the process is manual/outdated.
    """

class PrivateEquityDocs:
    company_capabilities_prompt = """
    Analyze the provided Company Capabilities document and extract key strategic insights:
    - Core competencies and specialized service offerings
    - Industry expertise and sector vertical strengths
    - Technological capabilities and proprietary tools/frameworks
    - Delivery model (onshore, offshore, hybrid) and scale
    - Key case studies and proven success patterns
    - Certifications, partnerships, and industry recognitions
    - Talent pool composition and specialized skills
    """
    
    pe_details_prompt = """
    Analyze the provided Private Equity / Portfolio document and extract key investment insights:
    - Investment thesis and strategic objectives for the portfolio
    - Growth expectations and target exit timelines
    - Specific value creation levers identified
    - Portfolio company synergies and integration opportunities
    - Financial performance targets and KPI expectations
    - Operational improvement priorities
    - Risk factors and mitigation strategies from the PE perspective
    """